"""Admin routes.

A Blueprint is Flask's way of collecting related URL handlers.  Defining a
route here does not start a second application: create_app() later registers
this collection on the one Flask application object.
"""

from flask import Blueprint

from .shared import (
    MAX_IMPORT_ROWS,
    MAX_PRIVACY_NOTICE_BYTES,
    Response,
    Role,
    audit_change,
    create_users,
    csv,
    csv_import_is_preview,
    csv_import_writes,
    create_rate_schedule,
    get_pagination,
    get_payload,
    get_search_query,
    g,
    io,
    jsonify,
    list_audit_events,
    list_rate_schedules,
    list_users,
    paginated_response,
    publish_privacy_notice,
    queue_csv_import,
    render_template,
    request,
    sqlite3,
)
from ..auth.decorators import recent_authentication_required, roles_required
from ..db import prepare_user_import, validate_user_import
from ..dtos import UserCreateDTO, UserUpdateDTO
from ..dtos.users import UserImportDTO
from ..core.validation import parse_dto
from ..services.workflows import UserAdministrationService, WorkflowNotFoundError


blueprint = Blueprint("admin", __name__)
user_administration_service = UserAdministrationService()


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
        "published",
        "privacy_notice_version",
        version["id"],
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
        rates = {field: int(payload[field]) for field in required[1:]}
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
        limit=limit, offset=offset, with_total=True, search=get_search_query()
    )
    return jsonify(paginated_response(items, total, limit, offset))


@blueprint.get("/admin/users")
@roles_required(Role.ADMIN)
def users_index():
    """List safe account metadata without returning password hashes."""
    limit, offset = get_pagination()
    items, total = list_users(limit=limit, offset=offset, with_total=True, search=get_search_query())
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
        normalised = dict(payload)
        if normalised.get("person_id") in (None, ""):
            normalised["person_id"] = None
        dto = parse_dto(UserCreateDTO, normalised)
        user = user_administration_service.create(
            dto.model_dump(),
            actor=g.current_user,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except sqlite3.IntegrityError:
        return jsonify({"error": "Username is already in use."}), 409
    return jsonify(user), 201


@blueprint.post("/admin/users/import")
@roles_required(Role.ADMIN)
@recent_authentication_required()
def users_import():
    """Queue a bounded background account import after recent authentication."""
    return queue_csv_import("users", _users_import_now, recent_auth_required=True)


def _users_import_now():
    """Validate an entire account CSV before atomically creating its accounts."""
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
        return jsonify(
            {"error": "The CSV file could not be read. Use UTF-8 encoding and valid CSV formatting."}
        ), 400

    required_fields = {"username", "role"}
    allowed_fields = required_fields | {"password", "person_id"}
    if not headers or not required_fields.issubset(headers):
        missing = sorted(required_fields - set(headers))
        return jsonify({"error": f"Missing required CSV column(s): {', '.join(missing)}."}), 400
    if len(headers) != len(set(headers)):
        return jsonify({"error": "The CSV contains duplicate column headings."}), 400
    unknown_fields = sorted(set(headers) - allowed_fields)
    if unknown_fields:
        return jsonify({"error": f"Unknown CSV column(s): {', '.join(unknown_fields)}."}), 400

    users = []
    errors = []
    try:
        for row_number, row in enumerate(reader, start=2):
            if row_number > MAX_IMPORT_ROWS + 1:
                raise ValueError(
                    f"The CSV contains more than {MAX_IMPORT_ROWS} data rows. Split it into smaller files."
                )
            if None in row:
                errors.append(f"Row {row_number} has more values than the CSV headings.")
                continue
            if not any(value and value.strip() for value in row.values() if isinstance(value, str)):
                continue
            person_value = (row.get("person_id") or "").strip()
            try:
                person_id = int(person_value) if person_value else None
                if person_id is not None and not 0 < person_id < 2**63:
                    raise ValueError("person_id is outside the supported range.")
                user = parse_dto(
                    UserImportDTO,
                    {
                        "username": (row.get("username") or "").strip(),
                        "password": row.get("password"),
                        "role": (row.get("role") or "").strip().lower(),
                        "person_id": person_id,
                    },
                ).model_dump()
            except ValueError as exc:
                errors.append(f"Row {row_number}: {exc}")
                continue
            users.append((row_number, user))
    except (csv.Error, ValueError) as exc:
        return jsonify({"error": str(exc) or "The CSV file is malformed."}), 400

    errors.extend(validate_user_import(users))
    if errors:
        return jsonify({"error": f"{errors[0]} No accounts were imported.", "errors": errors}), 400
    if not users:
        return jsonify({"error": "The CSV does not contain any accounts to import."}), 400
    if csv_import_is_preview():
        return jsonify({"preview": True, "valid_rows": len(users)})
    prepared = prepare_user_import([user for _, user in users])
    try:
        with csv_import_writes():
            created = create_users(prepared)
            for user in created:
                audit_change("created", "user", user["id"], ("username", "role", "person_id"))
    except sqlite3.IntegrityError:
        return jsonify({"error": "An account conflicts with existing data. No accounts were imported."}), 409
    return jsonify({"imported": len(created), "items": created}), 201


@blueprint.get("/admin/users/import/template")
@roles_required(Role.ADMIN)
def users_import_template():
    """Download the CSV header accepted by the account import."""
    return Response(
        "username,password,role,person_id\r\n",
        mimetype="text/csv",
        headers={"Content-Disposition": 'attachment; filename="accounts-template.csv"'},
    )


@blueprint.patch("/admin/users/<int:user_id>")
@roles_required(Role.ADMIN)
@recent_authentication_required()
def users_update(user_id: int):
    """Change account role, person association, or active status and audit it."""
    payload = get_payload()
    normalised = dict(payload)
    if normalised.get("person_id") == "":
        normalised["person_id"] = None
    dto = parse_dto(UserUpdateDTO, normalised)
    try:
        user = user_administration_service.update(
            user_id,
            dto.model_dump(exclude_unset=True),
            actor=g.current_user,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except WorkflowNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    return jsonify(user)
