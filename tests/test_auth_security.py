"""Authentication, authorization, sessions, response headers, audit integrity, and deployment security.

Each test receives a fresh temporary SQLite database from ``conftest.py``.
Route tests use real Flask clients and database users so permission checks,
request handling, and persistence are exercised together.
"""

from datetime import date
from io import BytesIO
import hashlib
import re
import sqlite3
from pathlib import Path

import pytest

import app as app_module
import audit_export
import club_gift_aid_manager.db as db_module
import club_gift_aid_manager.routing as routing_module
from app import app
from club_gift_aid_manager.config import get_declaration_text, update_declaration_text
from club_gift_aid_manager.crypto import encrypt_value
from club_gift_aid_manager.routing import RouteLookupError
from club_gift_aid_manager.db import (
    create_app_database,
    create_declaration,
    create_destination,
    create_donation,
    create_payment,
    create_person,
    create_rate_schedule,
    create_trip,
    create_user,
    deactivate_declaration,
    get_person,
    get_connection,
    get_user_by_id,
    get_trip,
    get_rate_schedule_for_date,
    get_summary,
    list_rate_schedules,
    list_audit_events,
    list_destinations,
    list_declarations,
    list_donations,
    list_payment_reconciliation,
    list_payments,
    list_people,
    save_destination_coordinates,
    update_donation,
)
from club_gift_aid_manager.services import calculate_tax_year, calculate_trip_amounts, validate_declaration


from tests.helpers import signed_in_client


def assert_sensitive_cache_headers(response):
    """Check the complete anti-caching policy used for sensitive responses.

    Keeping this assertion in the security module makes the expected browser
    behavior explicit while allowing HTML, JSON, redirects, downloads, and
    error-response tests to share the same checks.
    """
    assert response.headers["Cache-Control"] == "no-store, private, max-age=0"
    assert response.headers["Pragma"] == "no-cache"
    assert response.headers["Expires"] == "0"

def test_auth_blocks_unauthenticated_requests():
    """Unauthenticated API calls fail while browser page visits go to login."""
    client = app.test_client()
    assert client.get("/people").status_code == 401
    page_response = client.get("/destinations/page", headers={"Accept": "text/html"})
    assert page_response.status_code == 302
    assert page_response.location.endswith("/login")



def test_login_requires_csrf_and_authenticates_database_user():
    """Login requires CSRF and establishes an authenticated database-backed session."""
    create_user(username="admin-login", password="a-secure-test-password", role="admin")
    client = app.test_client()
    login_page = client.get("/login")
    assert login_page.status_code == 200
    with client.session_transaction() as browser_session:
        token = browser_session["_csrf_token"]

    missing_token = client.post(
        "/login",
        data={"username": "admin-login", "password": "a-secure-test-password"},
    )
    assert missing_token.status_code == 400
    success = client.post(
        "/login",
        data={"username": "admin-login", "password": "a-secure-test-password", "_csrf_token": token},
    )
    assert success.status_code == 302
    with client.session_transaction() as browser_session:
        assert browser_session["session_version"] == 1
    assert client.get("/people").status_code == 200
    login_events = [
        event for event in list_audit_events()
        if event["action"] == "login_succeeded"
    ]
    assert len(login_events) == 1



def test_role_change_invalidates_existing_session():
    """A session issued before a role change is rejected on its next request."""
    user = create_user(
        username="role-change-session",
        password="a-secure-test-password",
        role="treasurer",
    )
    client = app.test_client()
    with client.session_transaction() as browser_session:
        browser_session["user_id"] = user["id"]
        browser_session["session_version"] = user["session_version"]
        browser_session["_csrf_token"] = "test-csrf-token"

    updated = db_module.update_user(user["id"], role="admin")

    assert updated["session_version"] == user["session_version"] + 1
    response = client.get("/reports/summary")
    assert response.status_code == 401
    with client.session_transaction() as browser_session:
        assert "user_id" not in browser_session



def test_deactivation_invalidates_existing_page_session():
    """Deactivating an account clears its old session and uses the normal page redirect."""
    user = create_user(
        username="deactivated-session",
        password="a-secure-test-password",
        role="treasurer",
    )
    client = app.test_client()
    with client.session_transaction() as browser_session:
        browser_session["user_id"] = user["id"]
        browser_session["session_version"] = user["session_version"]
        browser_session["_csrf_token"] = "test-csrf-token"

    updated = db_module.update_user(user["id"], is_active=False)

    assert updated["session_version"] == user["session_version"] + 1
    response = client.get("/people/page")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")



def test_non_security_user_update_keeps_session_version():
    """Unchanged roles and non-revoking updates do not log a user out."""
    user = create_user(
        username="unchanged-session-version",
        password="a-secure-test-password",
        role="treasurer",
    )

    updated = db_module.update_user(user["id"], role="treasurer", is_active=True)

    assert updated["session_version"] == user["session_version"]



def test_login_rate_limit_is_persistent_and_does_not_store_raw_identifiers():
    """Repeated login failures are throttled using pseudonymous database keys."""
    client = app.test_client()
    client.get("/login")
    with client.session_transaction() as browser_session:
        token = browser_session["_csrf_token"]

    responses = [
        client.post(
            "/login",
            data={
                "username": "unknown-login-user",
                "password": "incorrect-test-password",
                "_csrf_token": token,
            },
        )
        for _ in range(5)
    ]

    assert [response.status_code for response in responses] == [401, 401, 401, 401, 429]
    with get_connection() as conn:
        attempts = conn.execute("SELECT attempt_key FROM login_attempts").fetchall()
    assert len(attempts) == 2
    assert all(len(row["attempt_key"]) == 64 for row in attempts)
    lockouts = [
        event for event in list_audit_events()
        if event["action"] == "login_rate_limited"
    ]
    failed_logins = [
        event for event in list_audit_events()
        if event["action"] == "login_failed"
    ]
    assert len(lockouts) == 1
    assert len(failed_logins) == 5
    assert "unknown-login-user" not in lockouts[0]["entity_id"]



def test_write_and_audit_event_roll_back_together_when_audit_fails(monkeypatch):
    """A failed audit insert cannot leave the corresponding business write committed."""
    def fail_audit(**_kwargs):
        raise RuntimeError("Simulated audit storage failure")

    monkeypatch.setattr(app_module, "record_audit_event", fail_audit)
    response = signed_in_client("admin").post(
        "/people",
        json={"name": "Must roll back"},
        headers={"X-CSRF-Token": "test-csrf-token"},
    )

    assert response.status_code == 500
    assert all(person["name"] != "Must roll back" for person in list_people())



def test_role_denial_is_recorded_as_a_security_audit_event():
    """A denied privilege escalation attempt is attributable without recording a URL ID."""
    person = create_person(name="Audit denial volunteer")
    response = signed_in_client("volunteer", person["id"]).get("/admin/audit")

    assert response.status_code == 403
    event = next(
        event for event in list_audit_events()
        if event["action"] == "permission_denied"
    )
    assert event["actor_user_id"] is not None
    assert event["entity_type"] == "authorization"
    assert event["entity_id"] == "audit_index"



def test_audit_events_are_immutable_in_the_database():
    """Database triggers reject attempts to rewrite or delete security history."""
    db_module.record_audit_event(
        actor_user_id=None,
        actor_username="security-test",
        action="test_event",
        entity_type="test",
        entity_id="immutable",
    )
    event_id = list_audit_events()[0]["id"]
    conn = get_connection()
    try:
        with pytest.raises(sqlite3.IntegrityError, match="audit events are immutable"):
            conn.execute("UPDATE audit_events SET action = 'changed' WHERE id = ?", (event_id,))
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="audit events are immutable"):
            conn.execute("DELETE FROM audit_events WHERE id = ?", (event_id,))
    finally:
        conn.rollback()
        conn.close()



def test_signed_audit_export_detects_tampering(monkeypatch):
    """A separately held HMAC key detects edits to exported audit evidence."""
    monkeypatch.setenv("GIFT_AID_AUDIT_EXPORT_KEY", "audit-export-test-key-that-is-long-enough")
    db_module.record_audit_event(
        actor_user_id=None,
        actor_username="export-test",
        action="test_event",
        entity_type="test",
        entity_id="signed",
    )

    document = audit_export.build_export(db_module.DB_PATH)

    assert audit_export.verify_export(document) is True
    document["events"][0]["action"] = "tampered"
    assert audit_export.verify_export(document) is False



def test_privileged_admin_change_requires_recent_authentication():
    """A valid but old session cannot alter account or security configuration."""
    client = signed_in_client("admin")
    with client.session_transaction() as browser_session:
        browser_session["authenticated_at"] = 0

    response = client.post(
        "/admin/users",
        json={"username": "blocked", "password": "long-enough-password", "role": "admin"},
        headers={"X-CSRF-Token": "test-csrf-token"},
    )

    assert response.status_code == 401
    assert response.get_json() == {
        "error": "Recent authentication is required. Sign out and sign in again."
    }



@pytest.mark.parametrize(
    ("environment", "secret", "secure_cookie", "valid"),
    [
        ("invalid", "x" * 48, True, False),
        ("production", None, True, False),
        ("production", "x" * 31, True, False),
        ("production", "x" * 48, False, False),
        ("production", "x" * 48, True, True),
        ("development", None, False, True),
    ],
)
def test_deployment_configuration_requires_secure_production_settings(
    environment, secret, secure_cookie, valid
):
    """Production requires a stable long secret and secure cookies."""
    if valid:
        app_module.validate_deployment_config(environment, secret, secure_cookie)
    else:
        with pytest.raises(RuntimeError):
            app_module.validate_deployment_config(environment, secret, secure_cookie)



def test_secure_session_cookie_and_security_headers_are_set(monkeypatch):
    """HTTPS deployments can emit secure cookies and baseline response headers."""
    monkeypatch.setitem(app.config, "SESSION_COOKIE_SECURE", True)
    create_user(username="secure-cookie-user", password="a-secure-test-password", role="admin")
    client = app.test_client()
    login_page = client.get("/login")
    with client.session_transaction() as browser_session:
        token = browser_session["_csrf_token"]
    response = client.post(
        "/login",
        data={
            "username": "secure-cookie-user",
            "password": "a-secure-test-password",
            "_csrf_token": token,
        },
    )

    assert "Secure" in response.headers["Set-Cookie"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "same-origin"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "Strict-Transport-Security" not in login_page.headers



def test_html_pages_have_unique_restrictive_csp_and_permissions_policy():
    """Rendered documents use per-request nonces and no unsafe or external sources."""
    client = signed_in_client()
    paths = [
        "/", "/how-it-works", "/mileage-rates/page", "/people/page",
        "/trips/page", "/destinations/page", "/declarations/page",
        "/donations/page", "/payments/page", "/users/page",
        "/admin/audit/page", "/reports/page", "/claim",
    ]
    nonces = set()
    for path in paths:
        response = client.get(path)
        assert response.status_code == 200
        assert response.mimetype == "text/html"
        policy = response.headers["Content-Security-Policy"]
        nonce = re.search(r"script-src 'self' 'nonce-([^']+)'", policy)
        assert nonce
        nonces.add(nonce.group(1))
        assert f"style-src 'self' 'nonce-{nonce.group(1)}'" in policy
        assert "'unsafe-inline'" not in policy
        assert "'unsafe-eval'" not in policy
        assert "https:" not in policy
        assert "http:" not in policy
        assert "object-src 'none'" in policy
        assert "frame-ancestors 'none'" in policy
        assert response.headers["Permissions-Policy"] == (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=(), "
            "accelerometer=(), gyroscope=(), magnetometer=()"
        )
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Referrer-Policy"] == "same-origin"
    assert len(nonces) == len(paths)

    login_one = app.test_client().get("/login")
    login_two = app.test_client().get("/login")
    first_nonce = re.search(r"'nonce-([^']+)'", login_one.headers["Content-Security-Policy"])
    second_nonce = re.search(r"'nonce-([^']+)'", login_two.headers["Content-Security-Policy"])
    assert first_nonce and second_nonce
    assert first_nonce.group(1) != second_nonce.group(1)



def test_csp_report_only_can_be_enabled(monkeypatch):
    """Operators can trial the same policy without disabling production enforcement defaults."""
    monkeypatch.setitem(app.config, "CSP_REPORT_ONLY", True)
    response = app.test_client().get("/login")
    assert "Content-Security-Policy" not in response.headers
    assert "Content-Security-Policy-Report-Only" in response.headers



def test_sensitive_cache_headers_cover_html_json_downloads_and_errors(monkeypatch):
    """Every non-static response type receives the overriding no-store policy."""
    admin = signed_in_client()
    assert_sensitive_cache_headers(admin.get("/"))
    assert_sensitive_cache_headers(admin.get("/reports/summary"))

    download = admin.get("/destinations/import/template")
    assert download.status_code == 200
    assert download.mimetype == "text/csv"
    assert "attachment;" in download.headers["Content-Disposition"]
    assert download.get_data(as_text=True).startswith("name,address,")
    assert_sensitive_cache_headers(download)

    login_page = app.test_client().get("/login")
    assert_sensitive_cache_headers(login_page)
    redirect_response = app.test_client().get("/")
    assert redirect_response.status_code == 302
    assert_sensitive_cache_headers(redirect_response)

    forbidden = signed_in_client("treasurer").get("/admin/audit")
    assert forbidden.status_code == 403
    assert_sensitive_cache_headers(forbidden)
    missing = admin.get("/not-a-real-route")
    assert missing.status_code == 404
    assert_sensitive_cache_headers(missing)

    def fail_audit(**_kwargs):
        raise RuntimeError("Simulated cache-header error response")

    monkeypatch.setattr(app_module, "record_audit_event", fail_audit)
    server_error = admin.post(
        "/people",
        json={"name": "Must roll back"},
        headers={"X-CSRF-Token": "test-csrf-token"},
    )
    assert server_error.status_code == 500
    assert_sensitive_cache_headers(server_error)



def test_sensitive_cache_policy_overrides_route_header_and_leaves_static_unchanged(monkeypatch):
    """A view cannot weaken the policy, while versioned static assets remain cacheable."""
    monkeypatch.setitem(
        app.view_functions,
        "reports_summary",
        lambda: app.response_class(
            '{"ok":true}',
            mimetype="application/json",
            headers={"Cache-Control": "public, max-age=3600"},
        ),
    )
    response = signed_in_client().get("/reports/summary")
    assert_sensitive_cache_headers(response)

    static_response = app.test_client().get("/static/css/styles.css")
    assert static_response.status_code == 200
    assert "no-store" not in static_response.headers.get("Cache-Control", "")
    assert "Pragma" not in static_response.headers
    assert "Expires" not in static_response.headers



def test_logout_clears_session_and_protected_history_cannot_return_data():
    """After logout, protected HTML and JSON require authentication again."""
    client = signed_in_client()
    protected = client.get("/reports/page")
    assert protected.status_code == 200
    assert "auth-history.js" in protected.get_data(as_text=True)

    logout_response = client.post(
        "/logout",
        headers={"X-CSRF-Token": "test-csrf-token"},
    )
    assert logout_response.status_code == 302
    assert_sensitive_cache_headers(logout_response)
    with client.session_transaction() as browser_session:
        assert "user_id" not in browser_session
        assert "_csrf_token" not in browser_session

    html_after_logout = client.get("/reports/page")
    assert html_after_logout.status_code in {302, 401, 403}
    if html_after_logout.status_code == 302:
        assert html_after_logout.headers["Location"].endswith("/login")
    else:
        assert b"Expenses paid" not in html_after_logout.data
    assert_sensitive_cache_headers(html_after_logout)
    json_after_logout = client.get("/reports/financial")
    assert json_after_logout.status_code == 401
    assert json_after_logout.get_json() == {"error": "Authentication required."}
    assert_sensitive_cache_headers(json_after_logout)



def test_mutating_api_rejects_missing_or_invalid_csrf_token():
    """State-changing API requests are rejected without the session CSRF token."""
    client = signed_in_client()
    response = client.post("/people", json={"name": "Not created"})
    assert response.status_code == 400
    assert response.get_json()["error"] == "CSRF token missing or invalid."



def test_privacy_notice_editor_requires_admin_and_valid_csrf():
    """Typed notice changes are protected by both role authorization and CSRF."""
    current = db_module.publish_privacy_notice("Current approved notice")
    notice_data = {"notice": "Replacement notice"}
    admin = signed_in_client()

    missing_token = admin.post("/admin/privacy-notice", json=notice_data)
    invalid_token = admin.post(
        "/admin/privacy-notice",
        json=notice_data,
        headers={"X-CSRF-Token": "invalid-token"},
    )
    forbidden = signed_in_client("treasurer").post(
        "/admin/privacy-notice",
        json=notice_data,
        headers={"X-CSRF-Token": "test-csrf-token"},
    )

    assert missing_token.status_code == 400
    assert invalid_token.status_code == 400
    assert forbidden.status_code == 403
    assert db_module.get_current_privacy_notice()["id"] == current["id"]



def test_route_distance_api_geocodes_caches_and_returns_road_miles(monkeypatch):
    """First route lookup geocodes both locations, stores coordinates, and routes."""
    origin = create_destination(name="Club Ground", address="Club Road", town_city="London")
    destination = create_destination(name="Away Ground", address="Rugby Lane", town_city="London")
    client = signed_in_client()
    request_urls = []
    responses = iter(
        [
            [{"lat": "51.5", "lon": "-0.1"}],
            [{"lat": "51.6", "lon": "-0.2"}],
            {"code": "Ok", "routes": [{"distance": 16093.44}]},
        ]
    )

    def fake_request_json(url):
        """Record the requested URL and return the next deterministic fixture."""
        request_urls.append(url)
        return next(responses)

    monkeypatch.setattr(routing_module, "_request_json", fake_request_json)
    monkeypatch.setattr(routing_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(routing_module, "_last_geocoder_request", 0.0)

    response = client.get(
        f"/api/route-distance?origin_id={origin['id']}&destination_id={destination['id']}"
    )

    assert response.status_code == 200
    result = response.get_json()
    assert result["distance_miles"] == 10.0
    assert result["provider"] == "OpenStreetMap / OSRM"
    assert len(request_urls) == 3
    assert db_module.get_destination(origin["id"])["latitude"] == 51.5
    assert db_module.get_destination(destination["id"])["longitude"] == -0.2



def test_production_database_must_be_absolute_and_outside_source_checkout(tmp_path):
    """Production refuses relative or checkout-local paths for live records."""
    project_directory = tmp_path / "source"
    project_directory.mkdir()
    with pytest.raises(RuntimeError, match="absolute"):
        app_module.validate_production_database_path(
            Path("relative.sqlite3"),
            project_directory,
        )
    with pytest.raises(RuntimeError, match="outside"):
        app_module.validate_production_database_path(
            project_directory / "data.sqlite3",
            project_directory,
        )
    app_module.validate_production_database_path(
        tmp_path / "persistent" / "data.sqlite3",
        project_directory,
    )



def test_hsts_is_production_only_and_reflects_explicit_config(monkeypatch):
    """HSTS stays off in development and adds optional directives only when enabled."""
    client = app.test_client()
    monkeypatch.setitem(app.config, "DEPLOYMENT_ENV", "development")
    assert "Strict-Transport-Security" not in client.get("/login").headers

    monkeypatch.setitem(app.config, "DEPLOYMENT_ENV", "production")
    monkeypatch.setitem(app.config, "HSTS_MAX_AGE", 12345)
    monkeypatch.setitem(app.config, "HSTS_INCLUDE_SUBDOMAINS", False)
    monkeypatch.setitem(app.config, "HSTS_PRELOAD", False)
    assert client.get("/login").headers["Strict-Transport-Security"] == "max-age=12345"

    monkeypatch.setitem(app.config, "HSTS_INCLUDE_SUBDOMAINS", True)
    monkeypatch.setitem(app.config, "HSTS_PRELOAD", True)
    assert client.get("/login").headers["Strict-Transport-Security"] == (
        "max-age=12345; includeSubDomains; preload"
    )



def test_templates_use_only_local_assets_and_have_no_unsafe_inline_code():
    """Template scans prevent CDN regressions and unnonced inline execution."""
    template_sources = [
        path.read_text(encoding="utf-8")
        for path in (Path(__file__).resolve().parent.parent / "templates").glob("*.html")
    ]
    template_sources.append(app_module.HTML_FORM)
    combined = "\n".join(template_sources)
    assert "cdn.jsdelivr.net" not in combined
    assert not re.search(
        r"<(?:script|link|img|source|video|audio|iframe|object|embed)\b[^>]*(?:src|href)=[\"']https?://",
        combined,
        re.IGNORECASE,
    )
    assert not re.search(r"\son[a-z]+\s*=", combined, re.IGNORECASE)
    assert not re.search(r"\sstyle\s*=", combined, re.IGNORECASE)
    assert not re.search(r"<style(?:\s|>)", combined, re.IGNORECASE)
    assert not re.search(r"<script(?![^>]*\bsrc\s*=)[^>]*>", combined, re.IGNORECASE)

    client = app.test_client()
    css = client.get("/static/vendor/bootstrap/5.3.3/bootstrap.min.css")
    javascript = client.get("/static/vendor/bootstrap/5.3.3/bootstrap.bundle.min.js")
    licence = client.get("/static/vendor/bootstrap/5.3.3/LICENSE")
    assert css.status_code == javascript.status_code == licence.status_code == 200
    assert b"Bootstrap  v5.3.3" in css.data
    assert b"Bootstrap v5.3.3" in javascript.data
    assert b"The MIT License" in licence.data



def test_trip_csv_template_headers_match_import_permissions():
    """Admins get an assignment column; non-admin templates do not."""
    admin = signed_in_client()
    admin_template = admin.get("/trips/import/template")
    assert admin_template.status_code == 200
    assert admin_template.mimetype == "text/csv"
    assert "attachment;" in admin_template.headers["Content-Disposition"]
    assert admin_template.get_data(as_text=True).strip().split(",") == [
        "person_id", "origin_id", "destination_id", "trip_date", "vehicle_type",
        "return_trip", "passengers", "declaration_accepted",
    ]
    trip_page = admin.get("/trips/page").get_data(as_text=True)
    assert 'href="/trips/import/template"' in trip_page
    assert 'id="trip-import-errors"' in trip_page

    person = create_person(name="Template user")
    volunteer = signed_in_client("volunteer", person["id"])
    volunteer_template = volunteer.get("/trips/import/template")
    assert volunteer_template.status_code == 200
    assert volunteer_template.get_data(as_text=True).strip().split(",") == [
        "origin_id", "destination_id", "trip_date", "vehicle_type",
        "return_trip", "passengers", "declaration_accepted",
    ]
    assert 'href="/trips/import/template"' in volunteer.get("/trips/page").get_data(as_text=True)



def test_location_csv_template_has_all_import_headers_and_is_role_gated():
    """Staff can download the location template; volunteers cannot."""
    admin = signed_in_client()
    response = admin.get("/destinations/import/template")
    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    assert "locations-template.csv" in response.headers["Content-Disposition"]
    assert response.get_data(as_text=True).strip().split(",") == [
        "name", "address", "town_city", "postcode", "notes", "home_ground",
    ]
    assert 'href="/destinations/import/template"' in admin.get("/destinations/page").get_data(as_text=True)

    person = create_person(name="Location template volunteer")
    volunteer = signed_in_client("volunteer", person["id"])
    assert volunteer.get("/destinations/import/template").status_code == 403



def test_every_named_template_field_has_specific_help_text():
    """Keep the field-help map aligned with every visible template form control."""
    template_dir = Path(__file__).resolve().parents[1] / "templates"
    help_script = (template_dir.parent / "static" / "js" / "help.js").read_text(encoding="utf-8")
    help_keys = set(re.findall(r"^\s*['\"]?([a-zA-Z0-9_-]+)['\"]?:", help_script, re.MULTILINE))
    field_names = set()
    for template in template_dir.glob("*.html"):
        source = template.read_text(encoding="utf-8")
        field_names.update(re.findall(r'\bname="([^"]+)"', source))

    field_names -= {"_csrf_token", "csrf-token", "viewport", "id"}
    assert field_names <= help_keys
    assert "document.querySelectorAll('input, select, textarea')" in help_script
    assert "new bootstrap.Popover(helpButton)" in help_script

