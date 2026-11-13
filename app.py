"""Flask web application for club mileage, Gift Aid, and payment records.

The request lifecycle first resolves the database-backed session user, then
enforces role access and CSRF protection. Page handlers render templates; API
handlers validate input, call the persistence/business layers, and return JSON.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import os
import secrets
import sqlite3
import tempfile
import time
from datetime import date, timedelta
from functools import wraps
from getpass import getpass
from pathlib import Path

from flask import Flask, Response, g, jsonify, redirect, render_template, render_template_string, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from club_gift_aid_manager.config import get_declaration_text
from club_gift_aid_manager.db import (
    create_app_database,
    create_declaration,
    create_destination,
    create_destinations,
    create_donation,
    create_payment,
    create_person,
    create_rate_schedule,
    create_trip,
    create_trips,
    create_user,
    anonymize_person,
    deactivate_declaration,
    begin_request_transaction,
    clear_login_failures,
    delete_destination,
    delete_trip,
    finish_request_transaction,
    get_login_lock_seconds,
    get_person,
    get_destination,
    get_declaration,
    get_financial_report,
    get_dashboard_checks,
    get_person_dashboard,
    get_rate_schedule_for_date,
    get_summary,
    get_trip,
    get_user_by_id,
    get_user_by_username,
    list_declarations,
    list_destinations,
    list_audit_events,
    list_donations,
    list_payment_reconciliation,
    list_payments,
    list_people,
    list_rate_schedules,
    list_trips,
    list_users,
    record_audit_event,
    record_login_failure,
    update_person,
    update_donation,
    update_declaration,
    update_destination,
    update_trip,
    update_user,
)
from club_gift_aid_manager.services import (
    calculate_tax_year,
    calculate_trip_amounts,
    rate_schedule_to_bands,
    validate_declaration,
)
from club_gift_aid_manager.routing import RouteLookupError, calculate_route_distance

# Cookie/session and upload-size defaults are centralized here. Production
# deployments should supply a stable secret and enable secure cookies over HTTPS.
app = Flask(__name__)
PRIVACY_NOTICE_PATH = Path(__file__).resolve().parent / "privacy-notice.txt"
MAX_PRIVACY_NOTICE_BYTES = 64 * 1024
DEPLOYMENT_ENV = os.environ.get("CLUB_GIFT_AID_ENV", "development").strip().lower()
configured_secret = os.environ.get("CLUB_GIFT_AID_SECRET")
secure_cookie = os.environ.get("CLUB_GIFT_AID_COOKIE_SECURE", "false").lower() in {"1", "true", "yes"}


def validate_deployment_config(environment, secret, cookie_secure):
    """Reject production configurations that could expose sessions or use an ephemeral key."""
    if environment not in {"development", "production"}:
        raise RuntimeError("CLUB_GIFT_AID_ENV must be 'development' or 'production'.")
    if environment == "production" and (
        not secret or len(secret) < 32 or not cookie_secure
    ):
        raise RuntimeError(
            "Production requires a stable CLUB_GIFT_AID_SECRET of at least 32 characters "
            "and CLUB_GIFT_AID_COOKIE_SECURE=true."
        )


def validate_production_database_path(database_path, project_directory):
    """Keep live records in an explicitly configured persistent path outside the checkout."""
    path = Path(database_path)
    if not path.is_absolute():
        raise RuntimeError("Production database path must be absolute.")
    try:
        path.resolve().relative_to(Path(project_directory).resolve())
    except ValueError:
        return
    raise RuntimeError("Production database path must be outside the source checkout.")


validate_deployment_config(DEPLOYMENT_ENV, configured_secret, secure_cookie)
if DEPLOYMENT_ENV == "production":
    configured_database_path = os.environ.get("CLUB_GIFT_AID_DB")
    if not configured_database_path:
        raise RuntimeError(
            "Production requires CLUB_GIFT_AID_DB to be an absolute path in persistent storage."
        )
    validate_production_database_path(configured_database_path, Path(__file__).resolve().parent)
app.config.update(
    SECRET_KEY=configured_secret or secrets.token_hex(32),
    DEPLOYMENT_ENV=DEPLOYMENT_ENV,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=secure_cookie,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    MAX_CONTENT_LENGTH=1024 * 1024,
)
DUMMY_PASSWORD_HASH = generate_password_hash(secrets.token_urlsafe(32))

HTML_FORM = """
{# Small standalone claim form retained for the /claim route. #}
<!doctype html>
<html lang=\"en\">
  <head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>Claim Mileage</title>
    <link href=\"https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css\" rel=\"stylesheet\" />
  </head>
  <body>
    <button class=\"btn btn-outline-secondary btn-sm\" type=\"button\" data-bs-toggle=\"modal\" data-bs-target=\"#page-instructions-modal\">Instructions</button>
    <h1>Volunteer mileage claim</h1>
    <form method=\"post\" action=\"/claim\">
      <input type=\"hidden\" name=\"_csrf_token\" value=\"{{ csrf_token }}\" />
      {% if current_user.role != 'volunteer' %}
      <label>Person ID <input type=\"number\" min=\"1\" name=\"person_id\" required /></label><br>
      {% endif %}
      <label>Origin
        <select name=\"origin_id\" required>
          <option value=\"\">Select an origin</option>
          {% for location in locations %}
          <option value=\"{{ location.id }}\" {{ 'selected' if location.home_ground else '' }}>{{ location.name }}</option>
          {% endfor %}
        </select>
      </label><br>
      <label>Trip date <input type=\"date\" name=\"trip_date\" required /></label><br>
      <label>Vehicle type
        <select name=\"vehicle_type\">
          <option value=\"car\">Car</option>
          <option value=\"motorcycle\">Motorcycle</option>
          <option value=\"bicycle\">Bicycle</option>
        </select>
      </label><br>
      <label>One-way miles <input type=\"number\" min=\"0\" step=\"0.1\" name=\"miles_one_way\" required /></label><br>
      <label>Return trip <input type=\"checkbox\" name=\"return_trip\" value=\"true\" /></label><br>
      <label>Passengers <input type=\"number\" min=\"0\" name=\"passengers\" value=\"0\" /></label><br>
      <label>
        <input type=\"checkbox\" name=\"declaration_accepted\" value=\"true\" />
        {{ declaration_text }}
      </label><br>
      <button type=\"submit\" {{ 'disabled' if not declaration_text else '' }}>Submit claim</button>
    </form>
    <div class=\"modal fade\" id=\"page-instructions-modal\" tabindex=\"-1\" aria-labelledby=\"page-instructions-title\" aria-hidden=\"true\">
      <div class=\"modal-dialog modal-dialog-centered\"><div class=\"modal-content\">
        <div class=\"modal-header\"><h2 class=\"modal-title fs-5\" id=\"page-instructions-title\">Trip claim instructions</h2><button type=\"button\" class=\"btn-close\" data-bs-dismiss=\"modal\" aria-label=\"Close\"></button></div>
        <div class=\"modal-body\"><p>Select the person (for staff), origin, trip date, vehicle, and one-way miles. Add passenger and return-trip details when relevant.</p><p class=\"mb-0\">Accept the declaration before submitting. Use the help button beside a field for an explanation.</p></div>
        <div class=\"modal-footer\"><button type=\"button\" class=\"btn btn-primary\" data-bs-dismiss=\"modal\">Got it</button></div>
      </div></div>
    </div>
    <button class=\"btn btn-outline-secondary btn-sm\" type=\"button\" data-bs-toggle=\"modal\" data-bs-target=\"#privacy-notice-modal\">Privacy / GDPR</button>
    <div class=\"modal fade\" id=\"privacy-notice-modal\" tabindex=\"-1\" aria-labelledby=\"privacy-notice-title\" aria-hidden=\"true\">
      <div class=\"modal-dialog modal-lg modal-dialog-scrollable\"><div class=\"modal-content\">
        <div class=\"modal-header\"><h2 class=\"modal-title fs-5\" id=\"privacy-notice-title\">Privacy statement</h2><button type=\"button\" class=\"btn-close\" data-bs-dismiss=\"modal\" aria-label=\"Close\"></button></div>
        <div class=\"modal-body\"><pre class=\"privacy-notice-text\" id=\"privacy-notice-content\">{{ privacy_notice }}</pre></div>
        {% if current_user and current_user.role == 'admin' %}
        <div class=\"modal-body border-top\">
          <form id=\"privacy-notice-editor-form\" class=\"mb-4\">
            <label class=\"form-label\" for=\"privacy-notice-editor\">Edit privacy statement</label>
            <textarea id=\"privacy-notice-editor\" class=\"form-control\" name=\"notice\" rows=\"12\" maxlength=\"65536\" required>{{ privacy_notice }}</textarea>
            <div class=\"form-text\">Enter the GDPR/privacy information to show to all users. Plain text only, up to 64 KB.</div>
            <button class=\"btn btn-primary btn-sm mt-2\" type=\"submit\">Save privacy statement</button>
          </form>
          <form id=\"privacy-notice-upload-form\">
            <label class=\"form-label\" for=\"privacy-notice-file\">Or replace from a text file</label>
            <input id=\"privacy-notice-file\" class=\"form-control\" type=\"file\" name=\"file\" accept=\".txt,text/plain\" required />
            <div class=\"form-text\">UTF-8 .txt file, up to 64 KB. The new statement is shown on all pages.</div>
            <button class=\"btn btn-primary btn-sm mt-2\" type=\"submit\">Upload statement</button>
          </form>
          <div id=\"privacy-notice-status\" class=\"small mt-2\" role=\"status\" aria-live=\"polite\"></div>
        </div>
        {% endif %}
        <div class=\"modal-footer\"><button type=\"button\" class=\"btn btn-light\" data-bs-dismiss=\"modal\">Close</button></div>
      </div></div>
    </div>
    <script src=\"https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js\"></script>
    <script src=\"{{ url_for('static', filename='js/help.js', v='1') }}\"></script>
    <script src=\"{{ url_for('static', filename='js/privacy.js', v='2') }}\"></script>
  </body>
</html>
"""


def get_payload():
    """Read request data as JSON when available, otherwise as form fields."""
    if request.is_json:
        return request.get_json(silent=True) or {}
    return request.form.to_dict(flat=True)


def audit_change(action, entity_type, entity_id, changed_fields=()):
    """Record audit metadata for the authenticated actor, never submitted values."""
    user = g.current_user
    record_audit_event(
        actor_user_id=user["id"],
        actor_username=user["username"],
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        changed_fields=changed_fields,
    )


def roles_required(*roles):
    """Wrap a route with server-side role authorization."""
    def decorator(view):
        """Create the wrapper for the endpoint being protected."""
        @wraps(view)
        def wrapped(*args, **kwargs):
            """Reject anonymous or disallowed users before the endpoint runs."""
            user = getattr(g, "current_user", None)
            if user is None:
                return jsonify({"error": "Authentication required."}), 401
            if user["role"] not in roles:
                record_audit_event(
                    actor_user_id=user["id"],
                    actor_username=user["username"],
                    action="permission_denied",
                    entity_type="authorization",
                    entity_id=request.endpoint or "unknown",
                    changed_fields=(request.method,),
                )
                return jsonify({"error": "You do not have permission to perform this action."}), 403
            return view(*args, **kwargs)
        return wrapped
    return decorator


@app.before_request
def enforce_security():
    """Load the session user, redirect page visitors, and check CSRF on writes.

    Login is handled before normal session loading because it is the operation
    that creates the session. API clients get JSON auth errors; page visitors
    are sent to the login screen.
    """
    html_endpoints = {
        "index",
        "how_it_works_page",
        "mileage_rates_page",
        "people_page",
        "trips_page",
        "destinations_page",
        "declarations_page",
        "donations_page",
        "payments_page",
        "audit_page",
        "users_page",
        "claim_page",
    }
    # Public static assets and login rendering do not yet have a signed-in user.
    if request.endpoint in {None, "static", "login_page"}:
        g.current_user = None
        return None

    # Login is checked before normal user resolution; otherwise the user would
    # need to be authenticated before the endpoint that authenticates them.
    if request.endpoint == "login":
        g.current_user = None
        expected = session.get("_csrf_token", "")
        supplied = request.headers.get("X-CSRF-Token") or request.form.get("_csrf_token", "")
        if not expected or not supplied or not hmac.compare_digest(expected, supplied):
            return jsonify({"error": "CSRF token missing or invalid."}), 400
        begin_request_transaction()
        return None

    # Treat a stale, missing, or deactivated account as an unauthenticated session.
    user_id = session.get("user_id")
    g.current_user = get_user_by_id(user_id) if isinstance(user_id, int) else None
    if g.current_user is None or not g.current_user["is_active"]:
        session.clear()
        if request.method == "GET" and request.endpoint in html_endpoints:
            return redirect(url_for("login_page"))
        if request.endpoint not in {"index", "claim_page"}:
            return jsonify({"error": "Authentication required."}), 401
        return redirect(url_for("login_page"))

    # Every state-changing browser request must prove it came from this session.
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        expected = session.get("_csrf_token", "")
        supplied = request.headers.get("X-CSRF-Token") or request.form.get("_csrf_token", "")
        if not expected or not supplied or not hmac.compare_digest(expected, supplied):
            return jsonify({"error": "CSRF token missing or invalid."}), 400
        begin_request_transaction()
    return None


@app.after_request
def finalize_request_transaction(response):
    """Commit successful writes and security events, rolling back failed operations."""
    commit_status = response.status_code < 400
    commit_security_denial = response.status_code == 403
    commit_login_failure = (
        request.endpoint == "login" and response.status_code in {401, 429}
    )
    finish_request_transaction(
        commit=commit_status or commit_security_denial or commit_login_failure
    )
    if DEPLOYMENT_ENV == "production":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    return response


@app.teardown_request
def rollback_unfinished_transaction(_error):
    """Ensure exceptions cannot leave an open or partially committed request transaction."""
    finish_request_transaction(commit=False)


@app.context_processor
def inject_security_context():
    """Make a stable CSRF token and the current user available to templates."""
    token = session.setdefault("_csrf_token", secrets.token_urlsafe(32))
    try:
        privacy_notice = PRIVACY_NOTICE_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        privacy_notice = "The club privacy statement has not been configured. Please contact the club administrator."
    return {
        "csrf_token": token,
        "current_user": getattr(g, "current_user", None),
        "privacy_notice": privacy_notice,
    }


@app.get("/login")
def login_page():
    """Render the sign-in form and its session-scoped CSRF token."""
    return render_template("login.html")


@app.post("/login")
def login():
    """Verify a password hash, rotate the session, and send the user to home."""
    username = request.form.get("username", "")
    password = request.form.get("password", "")
    attempt_keys = login_attempt_keys(username, request.remote_addr)
    now = int(time.time())
    lock_seconds = get_login_lock_seconds(attempt_keys, now=now)
    if lock_seconds:
        return render_template(
            "login.html",
            error="Incorrect username or password. Try again later.",
        ), 429

    user = get_user_by_username(username)
    password_matches = check_password_hash(
        user["password_hash"] if user else DUMMY_PASSWORD_HASH,
        password,
    )
    if user and user["is_active"] and password_matches:
        clear_login_failures(attempt_keys)
        session.clear()
        session["user_id"] = user["id"]
        session["_csrf_token"] = secrets.token_urlsafe(32)
        session.permanent = True
        record_audit_event(
            actor_user_id=user["id"],
            actor_username=user["username"],
            action="login_succeeded",
            entity_type="authentication",
            entity_id=user["id"],
        )
        return redirect(url_for("index"))

    newly_locked, lock_seconds = record_login_failure(
        attempt_keys,
        now=now,
        window_seconds=15 * 60,
        max_failures={attempt_keys[0]: 30, attempt_keys[1]: 5},
        lockout_seconds=15 * 60,
    )
    record_audit_event(
        actor_user_id=None,
        actor_username="anonymous",
        action="login_failed",
        entity_type="authentication",
        entity_id=attempt_keys[1],
    )
    if newly_locked:
        record_audit_event(
            actor_user_id=None,
            actor_username="anonymous",
            action="login_rate_limited",
            entity_type="authentication",
            entity_id=attempt_keys[0],
        )
    return render_template(
        "login.html",
        error=(
            "Incorrect username or password. Try again later."
            if lock_seconds
            else "Incorrect username or password."
        ),
    ), 429 if lock_seconds else 401


def login_attempt_keys(username, remote_address):
    """Derive non-reversible rate-limit keys without storing usernames or IP addresses."""
    normalized_user = username.strip().casefold()[:256]
    normalized_address = remote_address or "unknown"
    secret = app.config["SECRET_KEY"].encode("utf-8")
    values = (
        f"ip:{normalized_address}",
        f"ip-user:{normalized_address}:{normalized_user}",
    )
    return tuple(
        hmac.new(secret, value.encode("utf-8"), hashlib.sha256).hexdigest()
        for value in values
    )


@app.post("/logout")
def logout():
    """Discard login and CSRF state before returning to the sign-in page."""
    user = g.current_user
    record_audit_event(
        actor_user_id=user["id"],
        actor_username=user["username"],
        action="logout",
        entity_type="authentication",
        entity_id=user["id"],
    )
    session.clear()
    return redirect(url_for("login_page"))


@app.post("/admin/privacy-notice")
@roles_required("admin")
def privacy_notice_upload():
    """Replace the shared privacy statement from admin-entered text or an uploaded file."""
    upload = request.files.get("file")
    if upload is not None:
        if not upload.filename:
            return jsonify({"error": "Select a .txt file to upload."}), 400
        if not upload.filename.lower().endswith(".txt"):
            return jsonify({"error": "The uploaded file must have a .txt extension."}), 400
        content = upload.read(MAX_PRIVACY_NOTICE_BYTES + 1)
        if len(content) > MAX_PRIVACY_NOTICE_BYTES:
            return jsonify({"error": "The privacy statement must be 64 KB or smaller."}), 413
        try:
            notice = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            return jsonify({"error": "The privacy statement must be a UTF-8 text file."}), 400
    else:
        notice = request.form.get("notice")
        if notice is None:
            payload = request.get_json(silent=True) or {}
            notice = payload.get("notice")
        if not isinstance(notice, str):
            return jsonify({"error": "Enter the privacy statement before saving."}), 400
        if len(notice.encode("utf-8")) > MAX_PRIVACY_NOTICE_BYTES:
            return jsonify({"error": "The privacy statement must be 64 KB or smaller."}), 413
    if not notice.strip():
        return jsonify({"error": "The privacy statement cannot be empty."}), 400
    if "\x00" in notice:
        return jsonify({"error": "The privacy statement contains invalid text."}), 400

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=PRIVACY_NOTICE_PATH.parent,
            prefix=".privacy-notice-",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(notice)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, PRIVACY_NOTICE_PATH)
    except OSError:
        return jsonify({"error": "The privacy statement could not be saved. Check that the application can write to its installation folder."}), 500
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                app.logger.exception("Could not remove temporary privacy statement file.")

    audit_change("updated", "privacy_notice", "current", ("content",))
    return jsonify({"updated": True, "notice": notice})


@app.get("/")
def index():
    """Render the role-aware dashboard shell."""
    return render_template("index.html")


@app.get("/how-it-works")
@roles_required("admin", "treasurer", "volunteer")
def how_it_works_page():
    """Render the role-specific application guides."""
    return render_template("how_it_works.html")


@app.get("/mileage-rates/page")
@roles_required("admin")
def mileage_rates_page():
    """Render the administrator-only effective mileage rate schedule."""
    return render_template("mileage_rates.html")


@app.get("/people/page")
@roles_required("admin", "treasurer", "volunteer")
def people_page():
    """Render the people directory; API calls separately provide its records."""
    return render_template("people.html")


@app.get("/trips/page")
@roles_required("admin", "treasurer", "volunteer")
def trips_page():
    """Render the trip form with the exact declaration text to be accepted."""
    return render_template("trips.html", declaration_text=get_declaration_text())


@app.get("/destinations/page")
@roles_required("admin", "treasurer")
def destinations_page():
    """Render the staff-only location masterfile."""
    return render_template("destinations.html")


@app.get("/declarations/page")
@roles_required("admin", "treasurer")
def declarations_page():
    """Render the staff-only Gift Aid declaration page."""
    return render_template("declarations.html")


@app.get("/donations/page")
@roles_required("admin", "treasurer")
def donations_page():
    """Render donation management for finance roles."""
    return render_template("donations.html")


@app.get("/payments/page")
@roles_required("admin", "treasurer")
def payments_page():
    """Render the reimbursement and reconciliation page."""
    return render_template("payments.html")


@app.get("/users/page")
@roles_required("admin")
def users_page():
    """Render account-management controls for administrators."""
    return render_template("users.html")


@app.get("/admin/audit/page")
@roles_required("admin")
def audit_page():
    """Render the administrator-only activity history."""
    return render_template("audit.html")


@app.get("/reports/page")
@roles_required("admin")
def reports_page():
    """Render the administrator-only financial and volunteer report."""
    return render_template("reports.html")


@app.get("/claim")
def claim_page():
    """Render the lightweight claim form with active locations and home default."""
    return render_template_string(
        HTML_FORM,
        declaration_text=get_declaration_text(),
        locations=list_destinations(active_only=True),
    )


@app.post("/claim")
@roles_required("admin", "treasurer", "volunteer")
def submit_claim():
    """Validate a submitted claim, enforce volunteer ownership, and save it."""
    payload = get_payload()
    try:
        trip_date = date.fromisoformat(payload["trip_date"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "Trip date is required and must be ISO format."}), 400

    vehicle_type = payload.get("vehicle_type", "car")
    miles_one_way = float(payload.get("miles_one_way", 0) or 0)
    return_trip = str(payload.get("return_trip", "false")).lower() in {"true", "1", "yes"}
    passengers = int(payload.get("passengers", 0) or 0)
    declaration_accepted = str(payload.get("declaration_accepted", "false")).lower() in {"true", "1", "yes"}
    declaration_text = get_declaration_text()

    try:
        validate_declaration(declaration_accepted, declaration_text)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    # The request cannot choose a different owner for a volunteer's claim.
    person_id = g.current_user["person_id"] if g.current_user["role"] == "volunteer" else int(payload.get("person_id", 0))
    if not person_id:
        return jsonify({"error": "A person must be linked to this account or selected for the claim."}), 400
    if get_person(person_id) is None:
        return jsonify({"error": "Person not found."}), 404

    if not payload.get("origin_id"):
        return jsonify({"error": "A valid origin location is required."}), 400
    try:
        origin_id = int(payload["origin_id"])
    except (TypeError, ValueError):
        return jsonify({"error": "A valid origin location is required."}), 400
    try:
        trip = create_trip(
            person_id=person_id,
            origin_id=origin_id,
            vehicle_type=vehicle_type,
            trip_date=trip_date,
            miles_one_way=miles_one_way,
            return_trip=return_trip,
            passengers=passengers,
            declaration_accepted=True,
            declaration_text=declaration_text,
            tax_year=calculate_tax_year(trip_date),
        )
    except (KeyError, TypeError, ValueError) as exc:
        return jsonify({"error": str(exc) or "A valid origin location is required."}), 400
    audit_change("created", "trip", trip["id"], trip.keys())
    return jsonify({"status": "ok", "trip": trip}), 201


@app.get("/api/trip/<int:trip_id>")
@roles_required("admin", "treasurer", "volunteer")
def get_trip_detail(trip_id: int):
    """Return one trip while hiding another volunteer's trip as not found."""
    trip = get_trip(trip_id)
    if trip is None:
        return jsonify({"error": "Trip not found"}), 404
    if g.current_user["role"] == "volunteer" and trip["person_id"] != g.current_user["person_id"]:
        return jsonify({"error": "Trip not found"}), 404
    return jsonify(trip)


@app.get("/api/calculate")
@roles_required("admin", "treasurer", "volunteer")
def calculate_route():
    """Return a reimbursement breakdown from trip details supplied by the UI."""
    payload = request.args.to_dict()
    try:
        trip_date = date.fromisoformat(payload["trip_date"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "Trip date is required and must be ISO format."}), 400

    miles_one_way = float(payload.get("miles_one_way", 0) or 0)
    round_trip = str(payload.get("return_trip", "false")).lower() in {"true", "1", "yes"}
    passengers = int(payload.get("passengers", 0) or 0)
    vehicle_type = payload.get("vehicle_type", "car")
    # Use the journey date, not the current date or the date the claim is entered.
    rate_schedule = get_rate_schedule_for_date(trip_date)
    result = calculate_trip_amounts(
        vehicle_type=vehicle_type,
        trip_date=trip_date,
        miles_one_way=miles_one_way,
        return_trip=round_trip,
        passengers=passengers,
        total_business_miles=0.0,
        other_org_miles=0.0,
        rate_bands=rate_schedule_to_bands(rate_schedule),
    )
    result["rate_effective_from"] = rate_schedule["effective_from"]
    return jsonify(result)


@app.get("/api/route-distance")
@roles_required("admin", "treasurer", "volunteer")
def route_distance():
    """Calculate one-way road miles between two selected active locations."""
    origin_id = request.args.get("origin_id", type=int)
    destination_id = request.args.get("destination_id", type=int)
    if not origin_id or not destination_id:
        return jsonify({"error": "Select both an origin and a destination."}), 400
    try:
        return jsonify(calculate_route_distance(origin_id, destination_id))
    except RouteLookupError as exc:
        return jsonify({"error": str(exc)}), exc.status_code


@app.get("/people")
@roles_required("admin", "treasurer", "volunteer")
def people_index():
    """Return all people to staff, but only the signed-in person to volunteers."""
    # Apply privacy filtering here on the server, not just in the page template.
    if g.current_user["role"] == "volunteer":
        person = get_person(g.current_user["person_id"])
        return jsonify({"items": [person] if person else []})
    return jsonify({"items": list_people()})


@app.post("/people")
@roles_required("admin", "treasurer")
def people_create():
    """Validate required contact fields and create a person record."""
    payload = get_payload()
    required = ["name"]
    missing = [field for field in required if not payload.get(field)]
    if missing:
        return jsonify({"error": f"Missing required fields: {', '.join(missing)}"}), 400

    person = create_person(
        name=payload["name"],
        email=payload.get("email"),
        phone=payload.get("phone"),
        role=payload.get("role", "volunteer"),
        active=str(payload.get("active", "true")).lower() in {"true", "1", "yes"},
        notes=payload.get("notes"),
    )
    audit_change("created", "person", person["id"], person.keys())
    return jsonify(person), 201


@app.get("/people/<int:person_id>")
@roles_required("admin", "treasurer", "volunteer")
def people_get(person_id: int):
    """Fetch one person, applying the volunteer's own-record restriction."""
    if g.current_user["role"] == "volunteer" and person_id != g.current_user["person_id"]:
        return jsonify({"error": "Person not found"}), 404
    person = get_person(person_id)
    if person is None:
        return jsonify({"error": "Person not found"}), 404
    return jsonify(person)


@app.put("/people/<int:person_id>")
@roles_required("admin", "treasurer")
def people_update(person_id: int):
    """Edit a person and audit the changed field names."""
    payload = get_payload()
    if "active" in payload:
        payload["active"] = str(payload["active"]).lower() in {"true", "1", "yes", "on"}
    person = update_person(person_id, **payload)
    if person is None:
        return jsonify({"error": "Person not found"}), 404
    action = (
        "dormant" if payload.get("active") is False
        else "reactivated" if payload.get("active") is True
        else "updated"
    )
    audit_change(action, "person", person_id, payload.keys())
    return jsonify(person)


@app.delete("/people/<int:person_id>")
@roles_required("admin", "treasurer")
def people_delete(person_id: int):
    """Anonymize a person while retaining transactions and recording the action."""
    anonymized = anonymize_person(person_id)
    if not anonymized:
        return jsonify({"error": "Person not found"}), 404
    audit_change(
        "anonymized",
        "person",
        person_id,
        ("name", "email", "phone", "notes", "active", "role"),
    )
    return jsonify({"anonymized": True})


@app.get("/trips")
@roles_required("admin", "treasurer", "volunteer")
def trips_index():
    """List trips, scoped to the logged-in person for volunteer accounts."""
    # Passing a person ID scopes SQL results before they leave the database layer.
    person_id = g.current_user["person_id"] if g.current_user["role"] == "volunteer" else None
    return jsonify({"items": list_trips(person_id)})


@app.get("/destinations")
@roles_required("admin", "treasurer", "volunteer")
def destinations_index():
    """Return all locations to staff and only active trip choices to volunteers."""
    active_only = g.current_user["role"] == "volunteer"
    return jsonify({"items": list_destinations(active_only=active_only)})


@app.post("/destinations")
@roles_required("admin", "treasurer")
def destinations_create():
    """Create a location, optionally making it the club's single home ground."""
    payload = get_payload()
    if not payload.get("name"):
        return jsonify({"error": "Destination name is required."}), 400
    try:
        destination = create_destination(
            name=str(payload["name"]),
            address=payload.get("address"),
            town_city=payload.get("town_city"),
            postcode=payload.get("postcode"),
            notes=payload.get("notes"),
            home_ground=str(payload.get("home_ground", "false")).lower() in {"true", "1", "yes", "on"},
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except sqlite3.IntegrityError:
        return jsonify({"error": "A destination with that name already exists."}), 409
    audit_change("created", "destination", destination["id"], destination.keys())
    return jsonify(destination), 201


@app.post("/destinations/import")
@roles_required("admin", "treasurer")
def destinations_import():
    """Validate and import locations from a CSV file."""
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify({"error": "Select a CSV file to upload."}), 400
    if not upload.filename.lower().endswith(".csv"):
        return jsonify({"error": "The uploaded file must have a .csv extension."}), 400

    try:
        contents = upload.read().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(contents, newline=""), strict=True)
        headers = [header.strip().lower() if header else "" for header in (reader.fieldnames or [])]
        reader.fieldnames = headers
    except (UnicodeDecodeError, csv.Error):
        return jsonify({"error": "The CSV file could not be read. Use UTF-8 encoding and valid CSV formatting."}), 400

    allowed_fields = {"name", "address", "town_city", "postcode", "notes", "home_ground"}
    if not headers or "name" not in headers:
        return jsonify({"error": "The CSV must include a 'name' column. Location name is mandatory."}), 400
    if len(headers) != len(set(headers)):
        return jsonify({"error": "The CSV contains duplicate column headings."}), 400
    unknown_fields = sorted(set(headers) - allowed_fields)
    if unknown_fields:
        return jsonify({"error": f"Unknown CSV column(s): {', '.join(unknown_fields)}."}), 400

    location_rows = []
    try:
        for row_number, row in enumerate(reader, start=2):
            if None in row:
                raise ValueError(f"Row {row_number} has more values than the CSV headings.")
            if not any(value and value.strip() for value in row.values() if isinstance(value, str)):
                continue
            name = (row.get("name") or "").strip()
            if not name:
                raise ValueError(f"Row {row_number} is missing the mandatory name.")
            home_ground_value = (row.get("home_ground") or "").strip().lower()
            if home_ground_value not in {"", "true", "1", "yes", "on", "false", "0", "no", "off"}:
                raise ValueError(f"Row {row_number} has an invalid home_ground value; use yes or no.")
            home_ground = home_ground_value in {"true", "1", "yes", "on"}
            location_rows.append((row_number, {
                field: (row.get(field) or "").strip() or None
                for field in ("name", "address", "town_city", "postcode", "notes")
            } | {"home_ground": home_ground}))
    except (csv.Error, ValueError) as exc:
        return jsonify({"error": str(exc) or "The CSV file is malformed."}), 400

    if not location_rows:
        return jsonify({"error": "The CSV does not contain any locations to import."}), 400

    locations = []
    home_ground_rows = []
    skipped_duplicate_rows = []
    seen_names = set()
    ascii_lower = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
    for row_number, location in location_rows:
        name_key = location["name"].translate(ascii_lower)
        if name_key in seen_names:
            skipped_duplicate_rows.append(row_number)
            continue
        seen_names.add(name_key)
        locations.append(location)
        if location["home_ground"]:
            home_ground_rows.append(row_number)

    if len(home_ground_rows) > 1:
        return jsonify({"error": "Only one row may be marked as the club home ground."}), 400

    try:
        created = create_destinations(locations)
    except ValueError as exc:
        return jsonify({"error": f"{exc} No locations were imported."}), 409
    except sqlite3.IntegrityError:
        return jsonify({"error": "A location name already exists. No locations were imported."}), 409
    for destination in created:
        audit_change("created", "destination", destination["id"], destination.keys())
    return jsonify({
        "imported": len(created),
        "items": created,
        "skipped_duplicate_rows": skipped_duplicate_rows,
    }), 201


@app.get("/destinations/import/template")
@roles_required("admin", "treasurer")
def destinations_import_template():
    """Download the CSV header row accepted by the location import."""
    return Response(
        "name,address,town_city,postcode,notes,home_ground\r\n",
        mimetype="text/csv",
        headers={"Content-Disposition": 'attachment; filename="locations-template.csv"'},
    )


@app.put("/destinations/<int:destination_id>")
@roles_required("admin", "treasurer")
def destinations_update(destination_id: int):
    """Update a location and audit edits or active/dormant status changes."""
    payload = get_payload()
    if "active" in payload:
        payload["active"] = str(payload["active"]).lower() in {"true", "1", "yes", "on"}
    if "home_ground" in payload:
        payload["home_ground"] = str(payload["home_ground"]).lower() in {"true", "1", "yes", "on"}
    try:
        destination = update_destination(destination_id, **payload)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except sqlite3.IntegrityError:
        return jsonify({"error": "A destination with that name already exists."}), 409
    if destination is None:
        return jsonify({"error": "Destination not found."}), 404
    audit_change(
        "dormant" if payload.get("active") is False
        else "reactivated" if payload.get("active") is True
        else "updated",
        "destination",
        destination_id,
        payload.keys(),
    )
    return jsonify(destination)


@app.delete("/destinations/<int:destination_id>")
@roles_required("admin", "treasurer")
def destinations_delete(destination_id: int):
    """Remove an unused location; preserve locations referenced by trips."""
    try:
        deleted = delete_destination(destination_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409
    if not deleted:
        return jsonify({"error": "Destination not found."}), 404
    audit_change("deleted", "destination", destination_id)
    return jsonify({"deleted": True})


@app.post("/trips")
@roles_required("admin", "treasurer", "volunteer")
def trips_create():
    """Create a trip for the selected person only when the caller is an admin."""
    payload = get_payload()
    required = ["trip_date", "vehicle_type", "miles_one_way", "origin_id", "destination_id"]
    if g.current_user["role"] == "admin":
        required.append("person_id")
    missing = [field for field in required if payload.get(field) in (None, "")]
    if missing:
        return jsonify({"error": f"Missing required fields: {', '.join(missing)}"}), 400

    declaration_text = get_declaration_text()
    declaration_accepted = str(payload.get("declaration_accepted", "false")).lower() in {"true", "1", "yes"}
    try:
        validate_declaration(declaration_accepted, declaration_text)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    # Non-admin accounts can only create trips for their linked person.
    if g.current_user["role"] != "admin":
        person_id = g.current_user["person_id"]
        if not person_id:
            return jsonify({"error": "This account is not linked to a person."}), 403
    else:
        try:
            person_id = int(payload["person_id"])
        except (TypeError, ValueError):
            return jsonify({"error": "A valid person is required for this trip."}), 400
        if get_person(person_id) is None:
            return jsonify({"error": "Person not found."}), 404

    try:
        trip = create_trip(
            person_id=person_id,
            origin_id=int(payload["origin_id"]),
            destination_id=int(payload["destination_id"]),
            vehicle_type=payload["vehicle_type"],
            trip_date=date.fromisoformat(payload["trip_date"]),
            miles_one_way=float(payload["miles_one_way"]),
            return_trip=str(payload.get("return_trip", "false")).lower() in {"true", "1", "yes"},
            passengers=int(payload.get("passengers", 0) or 0),
            declaration_accepted=declaration_accepted,
            declaration_text=declaration_text,
            tax_year=calculate_tax_year(date.fromisoformat(payload["trip_date"])),
            status="submitted",
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    audit_change("created", "trip", trip["id"], trip.keys())
    return jsonify(trip), 201


@app.post("/trips/import")
@roles_required("admin", "treasurer", "volunteer")
def trips_import():
    """Validate and atomically import trip records from a CSV file."""
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify({"error": "Select a CSV file to upload."}), 400
    if not upload.filename.lower().endswith(".csv"):
        return jsonify({"error": "The uploaded file must have a .csv extension."}), 400

    try:
        contents = upload.read().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(contents, newline=""), strict=True)
        headers = [header.strip().lower() if header else "" for header in (reader.fieldnames or [])]
        reader.fieldnames = headers
    except (UnicodeDecodeError, csv.Error):
        return jsonify({"error": "The CSV file could not be read. Use UTF-8 encoding and valid CSV formatting."}), 400

    allowed_fields = {
        "person_id", "person_name", "origin_id", "destination_id", "trip_date",
        "vehicle_type", "miles_one_way", "return_trip", "passengers", "declaration_accepted",
    }
    if not headers:
        return jsonify({"error": "The CSV must include the required trip columns."}), 400
    if len(headers) != len(set(headers)):
        return jsonify({"error": "The CSV contains duplicate column headings."}), 400
    unknown_fields = sorted(set(headers) - allowed_fields)
    if unknown_fields:
        return jsonify({"error": f"Unknown CSV column(s): {', '.join(unknown_fields)}."}), 400
    if not {"origin_id", "destination_id", "trip_date", "vehicle_type", "declaration_accepted"} <= set(headers):
        return jsonify({
            "error": "Required columns are origin_id, destination_id, trip_date, vehicle_type, and declaration_accepted."
        }), 400

    is_admin = g.current_user["role"] == "admin"
    if is_admin and not ({"person_id", "person_name"} & set(headers)):
        return jsonify({"error": "Admin uploads must include a person_id or person_name column."}), 400
    if is_admin and {"person_id", "person_name"} <= set(headers):
        return jsonify({"error": "Use either person_id or person_name, not both."}), 400
    if not is_admin and ({"person_id", "person_name"} & set(headers)):
        return jsonify({"error": "Only administrators can assign trips to other people; remove the person_id/person_name column."}), 403
    if not is_admin and not g.current_user["person_id"]:
        return jsonify({"error": "This account is not linked to a person and cannot create trips."}), 403

    people_by_name: dict[str, list[dict]] = {}
    if is_admin and "person_name" in headers:
        for person in list_people():
            people_by_name.setdefault(person["name"].strip().casefold(), []).append(person)

    truthy_values = {"true", "1", "yes", "on"}
    falsey_values = {"false", "0", "no", "off"}
    parsed_rows = []
    errors = []
    try:
        for row_number, row in enumerate(reader, start=2):
            if None in row:
                errors.append(f"Row {row_number}: has more values than the CSV headings.")
                continue
            if not any(value and value.strip() for value in row.values() if isinstance(value, str)):
                continue

            required_fields = (
                "origin_id", "destination_id", "trip_date", "vehicle_type", "declaration_accepted",
            )
            missing = [field for field in required_fields if not (row.get(field) or "").strip()]
            if missing:
                errors.append(f"Row {row_number}: is missing required field(s): {', '.join(missing)}.")
                continue

            try:
                if is_admin:
                    if "person_id" in headers:
                        try:
                            person_id = int((row.get("person_id") or "").strip())
                        except ValueError as exc:
                            raise ValueError(f"Row {row_number}: must have a valid person_id.") from exc
                        if get_person(person_id) is None:
                            raise ValueError(f"Row {row_number}: references a person_id that does not exist.")
                    else:
                        person_name = (row.get("person_name") or "").strip()
                        matches = people_by_name.get(person_name.casefold(), [])
                        if not person_name or len(matches) != 1:
                            raise ValueError(
                                f"Row {row_number}: person_name must match exactly one person."
                            )
                        person_id = matches[0]["id"]
                else:
                    person_id = g.current_user["person_id"]
                trip_date = date.fromisoformat((row.get("trip_date") or "").strip())
                origin_id = int((row.get("origin_id") or "").strip())
                destination_id = int((row.get("destination_id") or "").strip())
                passengers = int((row.get("passengers") or "0").strip())
            except (TypeError, ValueError) as exc:
                message = str(exc)
                if not message.startswith(f"Row {row_number}:"):
                    message = f"Row {row_number}: has an invalid person, date, location ID, or passenger count."
                errors.append(message)
                continue
            vehicle_type = (row.get("vehicle_type") or "").strip().lower()
            if vehicle_type not in {"car", "motorcycle", "bicycle"}:
                errors.append(f"Row {row_number}: has an invalid vehicle_type; use car, motorcycle, or bicycle.")
                continue
            if passengers < 0:
                errors.append(f"Row {row_number}: passenger count must be non-negative.")
                continue

            return_value = (row.get("return_trip") or "false").strip().lower()
            declaration_value = (row.get("declaration_accepted") or "").strip().lower()
            if return_value not in truthy_values | falsey_values:
                errors.append(f"Row {row_number}: has an invalid return_trip value; use yes or no.")
                continue
            if declaration_value not in truthy_values:
                if declaration_value in falsey_values:
                    errors.append(f"Row {row_number}: must confirm declaration_accepted as yes.")
                else:
                    errors.append(f"Row {row_number}: has an invalid declaration_accepted value; use yes.")
                continue

            parsed_rows.append((row_number, {
                "person_id": person_id,
                "origin_id": origin_id,
                "destination_id": destination_id,
                "trip_date": trip_date,
                "vehicle_type": vehicle_type,
                "return_trip": return_value in truthy_values,
                "passengers": passengers,
                "declaration_accepted": True,
            }))
    except csv.Error as exc:
        errors.append(f"CSV file is malformed: {exc}")

    route_cache: dict[tuple[int, int], float | str] = {}
    route_error_status = 400
    trips = []
    for row_number, trip in parsed_rows:
        route_key = (trip["origin_id"], trip["destination_id"])
        if route_key not in route_cache:
            try:
                route_cache[route_key] = float(
                    calculate_route_distance(*route_key)["distance_miles"]
                )
            except RouteLookupError as exc:
                route_cache[route_key] = str(exc)
                route_error_status = max(route_error_status, exc.status_code)
            except (KeyError, TypeError, ValueError) as exc:
                route_cache[route_key] = str(exc) or "Unable to calculate the road distance."
        route_result = route_cache[route_key]
        if isinstance(route_result, str):
            errors.append(f"Row {row_number}: road distance could not be calculated: {route_result}")
            continue
        trips.append({**trip, "miles_one_way": route_result})

    if not trips:
        if errors:
            return jsonify({
                "error": "No trips were imported. Correct the listed CSV errors and try again.",
                "errors": errors,
            }), route_error_status
        return jsonify({"error": "The CSV does not contain any trips to import."}), 400

    if errors:
        return jsonify({
            "error": "No trips were imported. Correct the listed CSV errors and try again.",
            "errors": errors,
        }), route_error_status

    declaration_text = get_declaration_text()
    try:
        created = create_trips(
            trips,
            declaration_text=declaration_text,
        )
    except ValueError as exc:
        message = str(exc)
        return jsonify({"error": "No trips were imported.", "errors": [message]}), 400
    except sqlite3.IntegrityError:
        message = "A trip references an invalid record; no trips were imported."
        return jsonify({"error": "No trips were imported.", "errors": [message]}), 400
    for trip in created:
        audit_change("created", "trip", trip["id"], trip.keys())
    return jsonify({"imported": len(created), "items": created}), 201


@app.get("/trips/import/template")
@roles_required("admin", "treasurer", "volunteer")
def trips_import_template():
    """Download trip CSV headers appropriate to the current user's role."""
    headers = [
        "origin_id", "destination_id", "trip_date", "vehicle_type",
        "return_trip", "passengers", "declaration_accepted",
    ]
    if g.current_user["role"] == "admin":
        headers.insert(0, "person_id")
    return Response(
        ",".join(headers) + "\r\n",
        mimetype="text/csv",
        headers={"Content-Disposition": 'attachment; filename="trips-template.csv"'},
    )


@app.get("/trips/<int:trip_id>")
@roles_required("admin", "treasurer", "volunteer")
def trips_get(trip_id: int):
    """Return one trip, hiding records owned by another volunteer."""
    trip = get_trip(trip_id)
    if trip is None:
        return jsonify({"error": "Trip not found"}), 404
    if g.current_user["role"] == "volunteer" and trip["person_id"] != g.current_user["person_id"]:
        return jsonify({"error": "Trip not found"}), 404
    return jsonify(trip)


@app.put("/trips/<int:trip_id>")
@roles_required("admin", "treasurer")
def trips_update(trip_id: int):
    """Update an administrative trip record and note fields changed in the audit."""
    payload = get_payload()
    try:
        trip = update_trip(trip_id, **payload)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if trip is None:
        return jsonify({"error": "Trip not found"}), 404
    audit_change("updated", "trip", trip_id, payload.keys())
    return jsonify(trip)


@app.delete("/trips/<int:trip_id>")
@roles_required("admin", "treasurer")
def trips_delete(trip_id: int):
    """Delete an unpaid trip; the data layer rejects paid trips."""
    try:
        deleted = delete_trip(trip_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409
    if not deleted:
        return jsonify({"error": "Trip not found"}), 404
    audit_change("deleted", "trip", trip_id)
    return jsonify({"deleted": True})


@app.get("/declarations")
@roles_required("admin", "treasurer")
def declarations_index():
    """List Gift Aid declarations, optionally filtered to one person."""
    person_id = request.args.get("person_id", type=int)
    return jsonify({"items": list_declarations(person_id)})


@app.post("/declarations")
@roles_required("admin", "treasurer")
def declarations_create():
    """Create a donor declaration and record its creation in the audit log."""
    payload = get_payload()
    required = ["person_id", "full_name"]
    missing = [field for field in required if payload.get(field) in (None, "")]
    if missing:
        return jsonify({"error": f"Missing required fields: {', '.join(missing)}"}), 400

    declaration = create_declaration(
        person_id=int(payload["person_id"]),
        full_name=payload["full_name"],
        home_address=payload.get("home_address"),
        postcode=payload.get("postcode"),
        declaration_date=payload.get("declaration_date"),
        scope=payload.get("scope", "future"),
        withdrawn_date=payload.get("withdrawn_date"),
        evidence_ref=payload.get("evidence_ref"),
    )
    audit_change("created", "declaration", declaration["id"], declaration.keys())
    return jsonify(declaration), 201


@app.get("/declarations/<int:declaration_id>")
@roles_required("admin", "treasurer")
def declarations_get(declaration_id: int):
    """Fetch one declaration, including inactive records for staff review."""
    declaration = get_declaration(declaration_id)
    if declaration is None:
        return jsonify({"error": "Declaration not found."}), 404
    return jsonify(declaration)


@app.put("/declarations/<int:declaration_id>")
@roles_required("admin", "treasurer")
def declarations_update(declaration_id: int):
    """Edit a declaration or restore it, then record the changed field names."""
    payload = get_payload()
    if "active" in payload:
        payload["active"] = str(payload["active"]).lower() in {"true", "1", "yes", "on"}
    if "person_id" in payload:
        try:
            if get_person(int(payload["person_id"])) is None:
                return jsonify({"error": "Person not found."}), 404
        except (TypeError, ValueError):
            return jsonify({"error": "A valid person is required."}), 400
    try:
        declaration = update_declaration(declaration_id, **payload)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if declaration is None:
        return jsonify({"error": "Declaration not found."}), 404
    action = (
        "dormant" if payload.get("active") is False
        else "reactivated" if payload.get("active") is True
        else "updated"
    )
    audit_change(action, "declaration", declaration_id, payload.keys())
    return jsonify(declaration)


@app.delete("/declarations/<int:declaration_id>")
@roles_required("admin", "treasurer")
def declarations_delete(declaration_id: int):
    """Logically delete a declaration while preserving its historical evidence."""
    if not deactivate_declaration(declaration_id):
        return jsonify({"error": "Declaration not found."}), 404
    audit_change("dormant", "declaration", declaration_id, ("active",))
    return jsonify({"deleted": True, "active": False})


@app.get("/donations")
@roles_required("admin", "treasurer")
def donations_index():
    """List donations, including dormant items for administrative review."""
    person_id = request.args.get("person_id", type=int)
    return jsonify({"items": list_donations(person_id)})


@app.post("/donations")
@roles_required("admin", "treasurer")
def donations_create():
    """Create a donation after validating its person and optional associations."""
    payload = get_payload()
    required = ["person_id", "amount_pence"]
    missing = [field for field in required if payload.get(field) in (None, "")]
    if missing:
        return jsonify({"error": f"Missing required fields: {', '.join(missing)}"}), 400

    try:
        donation = create_donation(
            person_id=int(payload["person_id"]),
            amount_pence=int(payload["amount_pence"]),
            donated_date=payload.get("donated_date"),
            method=payload.get("method"),
            reference=payload.get("reference"),
            declaration_id=int(payload["declaration_id"]) if payload.get("declaration_id") else None,
            gift_aid_eligible=str(payload.get("gift_aid_eligible", "false")).lower() in {"true", "1", "yes"},
            gift_aid_claimed_date=payload.get("gift_aid_claimed_date"),
            linked_payment_ids=payload.get("linked_payment_ids") if "linked_payment_ids" in payload else None,
            linked_payment_id=int(payload["linked_payment_id"]) if payload.get("linked_payment_id") else None,
            received_date=payload.get("received_date"),
            bank_reference=payload.get("bank_reference"),
            receipt_id=payload.get("receipt_id"),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    audit_change("created", "donation", donation["id"], donation.keys())
    return jsonify(donation), 201


@app.put("/donations/<int:donation_id>")
@roles_required("admin", "treasurer")
def donations_update(donation_id: int):
    """Edit a donation or toggle its logical-active flag, then audit the change."""
    payload = get_payload()
    if "active" in payload:
        payload["active"] = str(payload["active"]).lower() in {"true", "1", "yes", "on"}
    for field in ("gift_aid_eligible",):
        if field in payload:
            payload[field] = str(payload[field]).lower() in {"true", "1", "yes", "on"}
    try:
        donation = update_donation(donation_id, **payload)
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    if donation is None:
        return jsonify({"error": "Donation not found."}), 404
    action = (
        "dormant" if payload.get("active") is False
        else "reactivated" if payload.get("active") is True
        else "updated"
    )
    audit_change(action, "donation", donation_id, payload.keys())
    return jsonify(donation)


@app.get("/payments")
@roles_required("admin", "treasurer")
def payments_index():
    """List reimbursements with their linked trips and donation totals."""
    person_id = request.args.get("person_id", type=int)
    return jsonify({"items": list_payments(person_id)})


@app.get("/admin/mileage-rates")
@roles_required("admin")
def mileage_rates_index():
    """Return all effective mileage rate schedules for administrators."""
    return jsonify({"items": list_rate_schedules()})


@app.post("/admin/mileage-rates")
@roles_required("admin")
def mileage_rates_create():
    """Add and audit a complete dated schedule without changing prior entries."""
    payload = get_payload()
    required = (
        "effective_from",
        "car_first_10000_pence",
        "car_over_10000_pence",
        "motorcycle_pence",
        "bicycle_pence",
        "passenger_pence",
    )
    # A schedule is an atomic set: partial updates cannot mix old and new rates.
    missing = [field for field in required if payload.get(field) in (None, "")]
    if missing:
        return jsonify({"error": f"Missing required fields: {', '.join(missing)}"}), 400
    try:
        rates = {
            field: int(payload[field])
            for field in required[1:]
        }
        if any(str(payload[field]).strip() != str(rates[field]) for field in required[1:]):
            raise ValueError("Rates must be entered as whole pence amounts.")
        schedule = create_rate_schedule(
            effective_from=str(payload["effective_from"]),
            **rates,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        return jsonify({"error": str(exc)}), 400
    except sqlite3.IntegrityError:
        return jsonify({"error": "A rate schedule already exists for that effective date."}), 409
    audit_change(
        "created",
        "mileage_rate_schedule",
        schedule["effective_from"],
        required,
    )
    return jsonify(schedule), 201


@app.post("/payments")
@roles_required("admin", "treasurer")
def payments_create():
    """Create a payment and optionally link multiple fully-covered trips."""
    payload = get_payload()
    required = ["person_id", "amount_pence"]
    missing = [field for field in required if payload.get(field) in (None, "")]
    if missing:
        return jsonify({"error": f"Missing required fields: {', '.join(missing)}"}), 400

    try:
        # Multi-select form values are serialized as a JSON list of trip IDs.
        trip_ids = payload.get("trip_ids", [])
        if not isinstance(trip_ids, list):
            return jsonify({"error": "Linked trips must be supplied as a list."}), 400
        payment = create_payment(
            person_id=int(payload["person_id"]),
            amount_pence=int(payload["amount_pence"]),
            paid_date=payload.get("paid_date"),
            method=payload.get("method", "bank transfer"),
            reference=payload.get("reference"),
            batch_id=payload.get("batch_id"),
            trip_ids=trip_ids,
        )
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    except sqlite3.IntegrityError:
        return jsonify({"error": "A selected trip has already been linked to another payment."}), 409
    audit_change("created", "payment", payment["id"], ("person_id", "amount_pence", "paid_date", "method", "reference", "trip_ids"))
    return jsonify(payment), 201


@app.get("/reports/summary")
@roles_required("admin", "treasurer")
def reports_summary():
    """Return high-level dashboard counters and financial totals."""
    return jsonify(get_summary())


@app.get("/dashboard/overview")
@roles_required("admin", "treasurer", "volunteer")
def dashboard_overview():
    """Return dashboard prompts scoped to the signed-in user's role."""
    if g.current_user["role"] == "volunteer":
        return jsonify(get_person_dashboard(g.current_user["person_id"]))

    overview = get_dashboard_checks()
    if g.current_user["role"] == "admin":
        overview["recent_activity"] = list_audit_events(limit=5)
    return jsonify(overview)


@app.get("/reports/payment-reconciliation")
@roles_required("admin", "treasurer")
def reports_payment_reconciliation():
    """Return per-person payments minus donations explicitly linked to payments."""
    return jsonify({"items": list_payment_reconciliation()})


@app.get("/reports/financial")
@roles_required("admin")
def reports_financial():
    """Return club totals and per-volunteer trip and finance aggregates."""
    return jsonify(get_financial_report())


@app.get("/admin/audit")
@roles_required("admin")
def audit_index():
    """Expose the bounded recent audit history to administrators only."""
    return jsonify({"items": list_audit_events()})


@app.get("/admin/users")
@roles_required("admin")
def users_index():
    """List safe account metadata without returning password hashes."""
    return jsonify({"items": list_users()})


@app.post("/admin/users")
@roles_required("admin")
def users_create():
    """Create an account; password hashing and role validation happen in the DB layer."""
    payload = get_payload()
    required = ("username", "password", "role")
    missing = [field for field in required if not payload.get(field)]
    if missing:
        return jsonify({"error": f"Missing required fields: {', '.join(missing)}"}), 400
    try:
        person_id = int(payload["person_id"]) if payload.get("person_id") not in (None, "") else None
        user = create_user(
            username=str(payload["username"]),
            password=str(payload["password"]),
            role=str(payload["role"]),
            person_id=person_id,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except sqlite3.IntegrityError:
        return jsonify({"error": "Username is already in use."}), 409
    audit_change("created", "user", user["id"], ("username", "role", "person_id"))
    return jsonify(user), 201


@app.patch("/admin/users/<int:user_id>")
@roles_required("admin")
def users_update(user_id: int):
    """Change account role, person association, or active status and audit it."""
    payload = get_payload()
    role = payload.get("role")
    active_value = payload.get("is_active")
    is_active = None
    if active_value is not None:
        is_active = str(active_value).lower() in {"true", "1", "yes"}
    try:
        user = update_user(
            user_id,
            role=role,
            person_id=int(payload["person_id"]) if payload.get("person_id") not in (None, "") else None,
            is_active=is_active,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if user is None:
        return jsonify({"error": "User not found."}), 404
    changed_fields = [field for field in ("role", "person_id", "is_active") if field in payload]
    audit_change(
        "deactivated" if is_active is False
        else "reactivated" if is_active is True and "is_active" in payload
        else "updated",
        "user",
        user_id,
        changed_fields,
    )
    return jsonify(user)


# Ensure the schema exists whenever the application module is loaded.
create_app_database()


if __name__ == "__main__":
    # This command creates the first admin interactively; normal execution
    # starts Flask. Password entry is hidden and never placed in source code.
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "create-admin":
        # Prevent accidental creation of a second bootstrap administrator.
        if list_users():
            raise SystemExit("Administrator accounts already exist; manage accounts as an administrator.")
        username = input("Admin username: ").strip()
        password = getpass("Admin password (12+ characters): ")
        try:
            user = create_user(username=username, password=password, role="admin")
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        except sqlite3.IntegrityError as exc:
            raise SystemExit("Username is already in use.") from exc
        print(f"Created administrator account {user['username']!r}.")
    else:
        if DEPLOYMENT_ENV == "production":
            raise SystemExit("Production deployments must use the configured WSGI server.")
        app.run(debug=os.environ.get("FLASK_DEBUG", "false").lower() in {"1", "true", "yes"})
