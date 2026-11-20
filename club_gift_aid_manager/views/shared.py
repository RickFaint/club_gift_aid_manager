"""Explicitly imported helpers shared by route modules.

This module supplies Flask helpers, database operations, and business services
through named imports so each Blueprint exposes its precise dependencies.
"""

import csv
import hashlib
import hmac
import io
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, timedelta
from getpass import getpass
from pathlib import Path

from flask import (
    Flask,
    Response,
    current_app,
    g,
    jsonify,
    redirect,
    render_template,
    render_template_string,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from club_gift_aid_manager.auth.decorators import authentication_is_recent
from club_gift_aid_manager.core.config import get_declaration_text
from club_gift_aid_manager.security.crypto import validate_encryption_config
from club_gift_aid_manager.db import (
    create_app_database,
    create_declaration,
    create_destination,
    create_destinations,
    create_donation,
    create_payment,
    create_person,
    create_people,
    create_rate_schedule,
    create_trip,
    create_trips,
    create_user,
    create_users,
    anonymize_person,
    deactivate_declaration,
    begin_request_transaction,
    clear_login_failures,
    delete_destination,
    delete_or_deactivate_person,
    delete_trip,
    finish_request_transaction,
    get_login_lock_seconds,
    get_person,
    get_current_privacy_notice,
    get_privacy_notice_version,
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
    record_audit_events,
    record_login_failure,
    publish_privacy_notice,
    quote_trip_expenses,
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
from club_gift_aid_manager.services.routing import (
    RouteLookupError,
    calculate_route_distance as _calculate_route_distance,
)
from club_gift_aid_manager.security.storage import validate_private_path
from club_gift_aid_manager.domain.roles import Role
from club_gift_aid_manager.core.validation import ValidationError, as_int


from club_gift_aid_manager.services.imports import ImportQueueFull, submit_import

MAX_PRIVACY_NOTICE_BYTES = 64 * 1024
# A row cap complements Flask's byte-size cap.  It prevents a compact CSV with
# thousands of tiny rows from monopolising a synchronous web worker.
MAX_IMPORT_ROWS = 10000


class ImportAuthorizationError(Exception):
    """Reject a queued operation whose authorization has expired or changed."""

    def __init__(self, message: str, status_code: int) -> None:
        """Carry a safe error response for the submitting browser."""
        super().__init__(message)
        self.status_code = status_code


def csv_import_is_preview() -> bool:
    """Indicate a validation-only upload."""
    return request.args.get("preview") == "1"


def _import_actor(submission):
    """Resolve the submitting account and reject stale permissions."""
    user_id, version, role, person_id, authenticated_at = submission
    actor = get_user_by_id(user_id)
    if (
        not actor
        or not actor["is_active"]
        or actor["session_version"] != version
        or actor["role"] != role
        or actor["person_id"] != person_id
    ):
        raise ImportAuthorizationError("Your account access changed. Submit the import again.", 403)
    if getattr(g, "csv_recent_auth_required", False) and not authentication_is_recent(
        authenticated_at
    ):
        raise ImportAuthorizationError("Sign in again before submitting an account import.", 401)
    return actor


@contextmanager
def csv_import_writes():
    """Enclose background persistence and its audit entries in one transaction."""
    submission = getattr(g, "csv_submission", None)
    if submission is None:
        # Synchronous dispatch already owns a transaction through Flask hooks.
        yield
        return
    begin_request_transaction()
    try:
        g.current_user = _import_actor(submission)
        yield
    except BaseException:
        finish_request_transaction(commit=False)
        raise
    else:
        finish_request_transaction(commit=True)


def queue_csv_import(kind: str, worker, *, recent_auth_required: bool = False):
    """Run an import now in tests or enqueue it for a background worker.

    Flask's ``request`` and ``g`` objects only live for the current request.
    Copy the upload bytes and the authorization markers before returning HTTP 202.
    The worker reloads the account before validation and again before writing.
    """
    if current_app.config.get("IMPORTS_SYNCHRONOUS", False):
        return worker()

    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify({"error": "Select a CSV file to upload."}), 400
    if not upload.filename.lower().endswith(".csv"):
        return jsonify({"error": "The uploaded file must have a .csv extension."}), 400

    filename = upload.filename
    contents = upload.read()
    application = current_app._get_current_object()
    user = g.current_user
    submission = (
        user["id"],
        user["session_version"],
        user["role"],
        user["person_id"],
        session.get("authenticated_at"),
    )
    query_string = {"preview": "1"} if csv_import_is_preview() else {}

    def perform_import():
        """Recreate request-local Flask state inside the background worker thread."""
        # Multipart data is reconstructed inside the worker because an upload
        # stream is closed when the original HTTP request ends.
        with application.test_request_context(
            method="POST",
            query_string=query_string,
            data={"file": (io.BytesIO(contents), filename)},
            content_type="multipart/form-data",
        ):
            g.csv_submission = submission
            g.csv_recent_auth_required = recent_auth_required
            try:
                g.current_user = _import_actor(submission)
                response = application.make_response(worker())
            except ImportAuthorizationError as exc:
                return {"error": str(exc)}, exc.status_code
            result = response.get_json(silent=True) or {
                "error": "The import returned an invalid response."
            }
            # Polling only needs counts and bounded validation feedback.
            result.pop("items", None)
            for field in ("errors", "skipped_duplicate_rows"):
                if isinstance(result.get(field), list):
                    result[f"{field}_count"] = len(result[field])
                    result[field] = result[field][:20]
            return result, response.status_code

    try:
        job = submit_import(submission[0], kind, perform_import)
    except ImportQueueFull:
        return jsonify({"error": "The import queue is busy. Try again shortly."}), 503
    return jsonify(
        {
            "job_id": job.id,
            "status": job.status,
            "status_url": url_for("main.import_status", job_id=job.id),
        }
    ), 202


DUMMY_PASSWORD_HASH = generate_password_hash(secrets.token_urlsafe(32))


def get_pagination() -> tuple[int, int]:
    """Read and validate the standard list-endpoint pagination parameters."""
    limit = as_int(request.args.get("limit", 50), "limit")
    offset = as_int(request.args.get("offset", 0), "offset")
    if limit < 1:
        raise ValidationError("limit must be at least 1.")
    if offset < 0:
        raise ValidationError("offset must be at least 0.")
    return min(limit, 200), offset


def paginated_response(items, total: int, limit: int, offset: int):
    """Build the common JSON-ready result used by every list endpoint."""
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def get_search_query() -> str:
    """Read a bounded directory query without interpreting it as a pattern."""
    search = request.args.get("q", "").strip()
    if len(search) > 200:
        raise ValidationError("Search must be 200 characters or fewer.")
    return search


def _compatibility_override(name, fallback):
    """Return a legacy root-module override when one has been installed.

    Earlier tests and deployments imported collaborators from ``app.py`` and
    sometimes replaced them for isolation.  Looking up that optional override
    at call time preserves that supported behavior after moving the routes.
    """
    import sys

    root_module = sys.modules.get("app")
    candidate = getattr(root_module, name, None) if root_module else None
    return candidate if candidate is not None and candidate is not globals().get(name) else fallback


def calculate_route_distance(*args, **kwargs):
    """Call the route service, honoring an optional legacy test override."""
    return _compatibility_override("calculate_route_distance", _calculate_route_distance)(
        *args, **kwargs
    )


_record_audit_event = record_audit_event


def record_audit_event(*args, **kwargs):
    """Write an audit event, honoring an optional legacy test override."""
    return _compatibility_override("record_audit_event", _record_audit_event)(*args, **kwargs)


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


def audit_created_batch(entity_type, entities):
    """Record one created event per imported entity in a single database write."""
    actor = g.current_user
    record_audit_events(
        actor_user_id=actor["id"],
        actor_username=actor["username"],
        events=(
            {
                "action": "created",
                "entity_type": entity_type,
                "entity_id": entity["id"],
                "changed_fields": entity.keys(),
            }
            for entity in entities
        ),
    )


HTML_FORM = """
{# Small standalone claim form retained for the /claim route. #}
<!doctype html>
<html lang=\"en\">
  <head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>Claim Mileage</title>
    <link href=\"{{ url_for('static', filename='vendor/bootstrap/5.3.3/bootstrap.min.css') }}\" rel=\"stylesheet\" />
  </head>
  <body>
    <button class=\"btn btn-outline-secondary btn-sm\" type=\"button\" data-bs-toggle=\"modal\" data-bs-target=\"#page-instructions-modal\">Instructions</button>
    <h1>Volunteer mileage claim</h1>
    <form method=\"post\" action=\"/claim\">
      <input type=\"hidden\" name=\"_csrf_token\" value=\"{{ csrf_token }}\" />
      <input type=\"hidden\" name=\"_idempotency_key\" value=\"{{ idempotency_key }}\" />
      {% if current_user.role == Role.ADMIN.value %}
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
      <label>Destination
        <select name=\"destination_id\" required>
          <option value=\"\">Select a destination</option>
          {% for location in locations %}
          <option value=\"{{ location.id }}\">{{ location.name }}</option>
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
      <p>One-way road mileage is calculated from the saved locations when you submit.</p>
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
        <div class=\"modal-body\"><p>Select the origin, destination, trip date, and vehicle. Administrators also select the person. Add passenger and return-trip details when relevant.</p><p class=\"mb-0\">Accept the declaration before submitting. Use the help button beside a field for an explanation.</p></div>
        <div class=\"modal-footer\"><button type=\"button\" class=\"btn btn-primary\" data-bs-dismiss=\"modal\">Got it</button></div>
      </div></div>
    </div>
    <button class=\"btn btn-outline-secondary btn-sm\" type=\"button\" data-bs-toggle=\"modal\" data-bs-target=\"#privacy-notice-modal\">Privacy / GDPR</button>
    <div class=\"modal fade\" id=\"privacy-notice-modal\" tabindex=\"-1\" aria-labelledby=\"privacy-notice-title\" aria-hidden=\"true\">
      <div class=\"modal-dialog modal-lg modal-dialog-scrollable\"><div class=\"modal-content\">
        <div class=\"modal-header\"><h2 class=\"modal-title fs-5\" id=\"privacy-notice-title\">Privacy statement</h2><button type=\"button\" class=\"btn-close\" data-bs-dismiss=\"modal\" aria-label=\"Close\"></button></div>
        <div class=\"modal-body\"><pre class=\"privacy-notice-text\" id=\"privacy-notice-content\">{{ privacy_notice }}</pre></div>
        {% if current_user and current_user.role == Role.ADMIN.value %}
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
    <script src=\"{{ url_for('static', filename='vendor/bootstrap/5.3.3/bootstrap.bundle.min.js') }}\"></script>
    <script src=\"{{ url_for('static', filename='js/help.js', v='1') }}\"></script>
    <script src=\"{{ url_for('static', filename='js/privacy.js', v='2') }}\"></script>
    <script src=\"{{ url_for('static', filename='js/auth-history.js', v='1') }}\"></script>
  </body>
</html>
"""
