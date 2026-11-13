"""Administrative users, mileage rates, privacy notices, pages, and help content.

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

def test_backdated_rate_schedule_closes_periods_without_replacing_later_schedule():
    """Adding a schedule between existing dates recomputes neighboring intervals."""
    rates = {
        "car_first_10000_pence": 60,
        "car_over_10000_pence": 30,
        "motorcycle_pence": 35,
        "bicycle_pence": 25,
        "passenger_pence": 10,
    }
    create_rate_schedule(effective_from="2027-01-01", **rates)
    create_rate_schedule(effective_from="2025-01-01", **rates)

    schedules = list_rate_schedules()

    assert [schedule["effective_from"] for schedule in schedules] == [
        "0001-01-01",
        "2025-01-01",
        "2027-01-01",
    ]
    assert [schedule["effective_to"] for schedule in schedules] == [
        "2024-12-31",
        "2026-12-31",
        None,
    ]
    assert get_rate_schedule_for_date(date(2026, 12, 31))["effective_from"] == "2025-01-01"
    assert get_rate_schedule_for_date(date(2027, 1, 1))["effective_from"] == "2027-01-01"



@pytest.mark.parametrize("effective_from", ["not-a-date", "2026-02-30", "0001-01-01"])
def test_rate_schedule_rejects_malformed_or_reserved_effective_dates(effective_from):
    """Malformed dates and the built-in baseline date cannot be overwritten."""
    with pytest.raises(ValueError, match="Effective date|original baseline"):
        create_rate_schedule(
            effective_from=effective_from,
            car_first_10000_pence=45,
            car_over_10000_pence=25,
            motorcycle_pence=24,
            bicycle_pence=20,
            passenger_pence=5,
        )



@pytest.mark.parametrize("invalid_rate", [-1, True, 1.5, 2**63])
def test_rate_schedule_rejects_negative_non_integer_and_unstorable_rates(invalid_rate):
    """Rate storage accepts only non-negative whole pence within SQLite integer range."""
    rates = {
        "car_first_10000_pence": 45,
        "car_over_10000_pence": 25,
        "motorcycle_pence": 24,
        "bicycle_pence": 20,
        "passenger_pence": 5,
    }
    rates["passenger_pence"] = invalid_rate

    with pytest.raises(ValueError):
        create_rate_schedule(effective_from="2027-01-01", **rates)



def test_admin_can_manage_rate_schedules_and_other_roles_cannot():
    """Only admins can view or add rates, and additions are audited."""
    admin = signed_in_client("admin")
    treasurer = signed_in_client("treasurer")
    volunteer_person = create_person(name="Rate schedule volunteer")
    volunteer = signed_in_client("volunteer", person_id=volunteer_person["id"])
    csrf = {"X-CSRF-Token": "test-csrf-token"}
    schedule_data = {
        "effective_from": "2027-01-01",
        "car_first_10000_pence": 50,
        "car_over_10000_pence": 25,
        "motorcycle_pence": 24,
        "bicycle_pence": 20,
        "passenger_pence": 5,
    }

    assert admin.get("/mileage-rates/page").status_code == 200
    assert treasurer.get("/mileage-rates/page").status_code == 403
    assert volunteer.get("/mileage-rates/page").status_code == 403
    assert treasurer.get("/admin/mileage-rates").status_code == 403
    assert volunteer.get("/admin/mileage-rates").status_code == 403

    created = admin.post("/admin/mileage-rates", json=schedule_data, headers=csrf)
    assert created.status_code == 201
    assert created.get_json()["car_first_10000_pence"] == 50
    duplicate = admin.post("/admin/mileage-rates", json=schedule_data, headers=csrf)
    assert duplicate.status_code == 409
    invalid = admin.post(
        "/admin/mileage-rates",
        json={**schedule_data, "bicycle_pence": -1},
        headers=csrf,
    )
    assert invalid.status_code == 400
    missing = admin.post(
        "/admin/mileage-rates",
        json={"effective_from": "2028-01-01"},
        headers=csrf,
    )
    assert missing.status_code == 400
    fractional = admin.post(
        "/admin/mileage-rates",
        json={**schedule_data, "car_first_10000_pence": 50.5},
        headers=csrf,
    )
    assert fractional.status_code == 400
    assert admin.get("/admin/mileage-rates").get_json()["items"][-1]["effective_from"] == "2027-01-01"

    audited = [
        event for event in admin.get("/admin/audit").get_json()["items"]
        if event["entity_type"] == "mileage_rate_schedule"
    ]
    assert len(audited) == 1
    assert audited[0]["action"] == "created"



def test_admin_can_create_users_and_treasurer_cannot_manage_users():
    """Only admins can create or list application user accounts."""
    admin = signed_in_client()
    response = admin.post(
        "/admin/users",
        json={"username": "treasurer", "password": "another-secure-password", "role": "treasurer"},
        headers={"X-CSRF-Token": "test-csrf-token"},
    )
    assert response.status_code == 201
    assert "password_hash" not in response.get_json()
    users = admin.get("/admin/users").get_json()["items"]
    assert next(user for user in users if user["username"] == "treasurer")["role"] == "treasurer"

    treasurer = signed_in_client("treasurer")
    assert treasurer.get("/admin/users").status_code == 403



def test_each_masterfile_has_its_own_page_with_role_checks():
    """Each masterfile page loads for allowed roles and is blocked otherwise."""
    treasurer = signed_in_client("treasurer")
    for path in (
        "/",
        "/people/page",
        "/trips/page",
        "/destinations/page",
        "/declarations/page",
        "/donations/page",
        "/payments/page",
    ):
        response = treasurer.get(path)
        assert response.status_code == 200, path
        assert b"Club Gift Aid Manager" in response.data

    volunteer = create_person(name="Page volunteer", role="volunteer")
    volunteer_user = signed_in_client("volunteer", volunteer["id"])
    assert volunteer_user.get("/trips/page").status_code == 200
    assert volunteer_user.get("/people/page").status_code == 200
    assert volunteer_user.get("/donations/page").status_code == 403

    admin = signed_in_client("admin")
    assert admin.get("/users/page").status_code == 200



def test_pages_render_instructions_and_shared_field_help():
    """Every application page exposes instructions and loads field-help behavior."""
    admin = signed_in_client()
    for path in (
        "/",
        "/claim",
        "/people/page",
        "/trips/page",
        "/destinations/page",
        "/declarations/page",
        "/donations/page",
        "/payments/page",
        "/users/page",
        "/admin/audit/page",
    ):
        response = admin.get(path)
        assert response.status_code == 200
        page = response.get_data(as_text=True)
        assert 'id="page-instructions-modal"' in page
        assert ">Instructions</button>" in page
        assert "js/help.js" in page

        assert 'id="privacy-notice-modal"' in page
        assert "Privacy / GDPR" in page
        assert "js/privacy.js" in page

    login_page = admin.get("/login").get_data(as_text=True)
    assert 'id="page-instructions-modal"' in login_page
    assert "js/help.js" in login_page
    assert 'id="privacy-notice-modal"' in login_page
    assert "Privacy / GDPR" in login_page
    assert "js/privacy.js" in login_page



def test_admin_can_replace_privacy_notice_with_text_file():
    """Admins can publish uploaded notice text as a new immutable version."""
    old_version = db_module.get_current_privacy_notice()
    admin = signed_in_client()

    response = admin.post(
        "/admin/privacy-notice",
        data={"file": (BytesIO(b"Updated <script>alert(1)</script>\nPrivacy info"), "notice.txt")},
        headers={"X-CSRF-Token": "test-csrf-token"},
    )

    assert response.status_code == 200
    new_version = db_module.get_current_privacy_notice()
    assert new_version["id"] > old_version["id"]
    assert new_version["notice_text"] == "Updated <script>alert(1)</script>\nPrivacy info"
    page = admin.get("/").get_data(as_text=True)
    assert "Updated &lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert any(
        event["entity_type"] == "privacy_notice_version" and event["action"] == "published"
        for event in admin.get("/admin/audit").get_json()["items"]
    )



def test_admin_can_type_and_save_privacy_notice():
    """Admins append typed notice text; saved content remains escaped plain text."""
    db_module.publish_privacy_notice("Old notice")
    admin = signed_in_client()
    editor_page = admin.get("/").get_data(as_text=True)
    assert 'id="privacy-notice-editor"' in editor_page
    assert "Old notice" in editor_page
    assert "Save privacy statement" in editor_page
    assert 'id="privacy-notice-editor"' not in signed_in_client("treasurer").get("/").get_data(as_text=True)

    updated_notice = "Controller: Club <script>alert('no')</script>\nRetention and GDPR details."
    response = admin.post(
        "/admin/privacy-notice",
        json={"notice": updated_notice},
        headers={"X-CSRF-Token": "test-csrf-token"},
    )

    assert response.status_code == 200
    assert response.get_json()["notice"] == updated_notice
    assert db_module.get_current_privacy_notice()["notice_text"] == updated_notice
    rendered = admin.get("/").get_data(as_text=True)
    assert "&lt;script&gt;" in rendered
    assert "<script>alert('no')</script>" not in rendered
    assert any(
        event["entity_type"] == "privacy_notice_version" and event["action"] == "published"
        for event in admin.get("/admin/audit").get_json()["items"]
    )



def test_privacy_notice_editor_enforces_utf8_byte_limit():
    """The notice limit applies to encoded bytes, including multibyte characters."""
    db_module.publish_privacy_notice("Current approved notice")
    admin = signed_in_client()
    csrf = {"X-CSRF-Token": "test-csrf-token"}
    max_size_notice = "Â£" * (64 * 1024 // len("Â£".encode("utf-8")))
    assert len(max_size_notice.encode("utf-8")) == 64 * 1024

    accepted = admin.post(
        "/admin/privacy-notice",
        json={"notice": max_size_notice},
        headers=csrf,
    )
    oversized = admin.post(
        "/admin/privacy-notice",
        json={"notice": max_size_notice + "Â£"},
        headers=csrf,
    )

    assert accepted.status_code == 200
    assert accepted.get_json()["notice"] == max_size_notice
    assert oversized.status_code == 413
    assert db_module.get_current_privacy_notice()["notice_text"] == max_size_notice



@pytest.mark.parametrize(
    ("notice", "status_code"),
    [
        pytest.param("", 400, id="empty"),
        pytest.param(" " * 4, 400, id="whitespace"),
        pytest.param("x" * (64 * 1024 + 1), 413, id="oversized"),
    ],
)
def test_admin_privacy_notice_editor_rejects_empty_or_oversized_text(notice, status_code):
    """Typed notice validation leaves the current statement unchanged on error."""
    current = db_module.publish_privacy_notice("Current approved notice")

    response = signed_in_client().post(
        "/admin/privacy-notice",
        json={"notice": notice},
        headers={"X-CSRF-Token": "test-csrf-token"},
    )

    assert response.status_code == status_code
    assert db_module.get_current_privacy_notice()["id"] == current["id"]



def test_non_admin_cannot_upload_privacy_notice_and_bad_upload_preserves_current_text():
    """Notice edits require admin permissions and invalid files do not replace the text."""
    current = db_module.publish_privacy_notice("Existing privacy notice")
    treasurer = signed_in_client("treasurer")

    forbidden = treasurer.post(
        "/admin/privacy-notice",
        data={"file": (BytesIO(b"replacement"), "notice.txt")},
        headers={"X-CSRF-Token": "test-csrf-token"},
    )
    assert forbidden.status_code == 403

    invalid = signed_in_client().post(
        "/admin/privacy-notice",
        data={"file": (BytesIO(b"not a text file"), "notice.csv")},
        headers={"X-CSRF-Token": "test-csrf-token"},
    )
    assert invalid.status_code == 400
    assert db_module.get_current_privacy_notice()["id"] == current["id"]



def test_privacy_notice_versions_are_hashed_historical_and_immutable():
    """Published text has verifiable provenance and old rows cannot be changed."""
    first = db_module.publish_privacy_notice(
        "First approved notice", effective_date="2026-01-01"
    )
    second = db_module.publish_privacy_notice(
        "Second approved notice", effective_date="2026-06-01"
    )

    assert first["text_sha256"] == hashlib.sha256(
        first["notice_text"].encode("utf-8")
    ).hexdigest()
    assert db_module.get_current_privacy_notice()["id"] == second["id"]
    historical = app.test_client().get(f"/privacy-notice/{first['id']}")
    assert historical.status_code == 200
    assert historical.get_json()["notice_text"] == "First approved notice"

    conn = get_connection()
    try:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE privacy_notice_versions SET notice_text = ? WHERE id = ?",
                ("Altered", first["id"]),
            )
    finally:
        conn.rollback()
        conn.close()

