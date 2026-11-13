"""Admin routes.

A Blueprint is Flask's way of collecting related URL handlers.  Defining a
route here does not start a second application: create_app() later registers
this collection on the one Flask application object.
"""

from flask import Blueprint

from .shared import *  # noqa: F403 - shared view vocabulary is intentionally centralised
from ..auth.decorators import recent_authentication_required, roles_required


blueprint = Blueprint("admin", __name__)

@blueprint.post("/admin/privacy-notice")
@roles_required(Role.ADMIN)
@recent_authentication_required()
def privacy_notice_upload():
    """Publish a new immutable privacy statement version from text or a text file."""
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

    try:
        payload = request.get_json(silent=True) or {} if request.is_json else request.form
        version = publish_privacy_notice(
            notice,
            effective_date=payload.get("effective_date") or None,
        )
    except ValueError:
        return jsonify({"error": "The effective date must use YYYY-MM-DD format."}), 400

    audit_change(
        "published", "privacy_notice_version", version["id"],
        ("effective_date", "published_date", "text_sha256", "notice_text"),
    )
    return jsonify({"updated": True, "notice": notice, "version": version})


@blueprint.get("/mileage-rates/page")
@roles_required(Role.ADMIN)
def mileage_rates_page():
    """Render the administrator-only effective mileage rate schedule."""
    return render_template("mileage_rates.html")


@blueprint.get("/users/page")
@roles_required(Role.ADMIN)
def users_page():
    """Render account-management controls for administrators."""
    return render_template("users.html")


@blueprint.get("/admin/audit/page")
@roles_required(Role.ADMIN)
def audit_page():
    """Render the administrator-only activity history."""
    return render_template("audit.html")


@blueprint.get("/admin/mileage-rates")
@roles_required(Role.ADMIN)
def mileage_rates_index():
    """Return all effective mileage rate schedules for administrators."""
    return jsonify({"items": list_rate_schedules()})


@blueprint.post("/admin/mileage-rates")
@roles_required(Role.ADMIN)
@recent_authentication_required()
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


@blueprint.get("/admin/audit")
@roles_required(Role.ADMIN)
def audit_index():
    """Expose the bounded recent audit history to administrators only."""
    limit, offset = get_pagination()
    items, total = list_audit_events(
        limit=limit, offset=offset, with_total=True
    )
    return jsonify(paginated_response(items, total, limit, offset))


@blueprint.get("/admin/users")
@roles_required(Role.ADMIN)
def users_index():
    """List safe account metadata without returning password hashes."""
    limit, offset = get_pagination()
    items, total = list_users(
        limit=limit, offset=offset, with_total=True
    )
    return jsonify(paginated_response(items, total, limit, offset))


@blueprint.post("/admin/users")
@roles_required(Role.ADMIN)
@recent_authentication_required()
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


@blueprint.patch("/admin/users/<int:user_id>")
@roles_required(Role.ADMIN)
@recent_authentication_required()
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


