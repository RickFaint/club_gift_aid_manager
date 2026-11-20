"""People routes.

A Blueprint is Flask's way of collecting related URL handlers.  Defining a
route here does not start a second application: create_app() later registers
this collection on the one Flask application object.
"""

from flask import Blueprint

from ..db import list_person_choices

from .shared import (
    MAX_IMPORT_ROWS,
    Response,
    Role,
    audit_change,
    create_person,
    create_people,
    delete_or_deactivate_person,
    csv,
    csv_import_is_preview,
    csv_import_writes,
    g,
    get_pagination,
    get_payload,
    get_person,
    get_search_query,
    io,
    jsonify,
    list_declarations,
    list_donations,
    list_payments,
    list_people,
    list_trips,
    paginated_response,
    queue_csv_import,
    render_template,
    request,
    update_person,
)
from ..auth.decorators import roles_required
from ..domain.policies import can_view_person
from ..core.validation import as_bool, as_email, as_text, require_fields
from ..dtos import PersonCreateDTO, PersonUpdateDTO
from ..core.validation import parse_dto


blueprint = Blueprint("people", __name__)


@blueprint.get("/people/page")
@roles_required(Role.ADMIN, Role.TREASURER, Role.VOLUNTEER)
def people_page():
    """Render the people directory; API calls separately provide its records."""
    return render_template("people.html")


@blueprint.get("/people")
@roles_required(Role.ADMIN, Role.TREASURER, Role.VOLUNTEER)
def people_index():
    """Return all people to staff, but only the signed-in person to volunteers."""
    # Apply privacy filtering here on the server, not just in the page template.
    limit, offset = get_pagination()
    person_id = None
    if g.current_user["role"] == Role.VOLUNTEER.value:
        person_id = g.current_user["person_id"]
        if person_id is None:
            return jsonify(paginated_response([], 0, limit, offset))
    items, total = list_people(
        limit=limit, offset=offset, with_total=True, search=get_search_query(), person_id=person_id
    )
    return jsonify(paginated_response(items, total, limit, offset))


@blueprint.get("/people/choices")
@roles_required(Role.ADMIN, Role.TREASURER, Role.VOLUNTEER)
def people_choices():
    """Return lightweight person labels while preserving volunteer ownership."""
    if g.current_user["role"] == Role.VOLUNTEER.value:
        person_id = g.current_user["person_id"]
        return jsonify({"items": list_person_choices(person_id) if person_id else []})
    return jsonify({"items": list_person_choices()})


@blueprint.post("/people")
@roles_required(Role.ADMIN, Role.TREASURER)
def people_create():
    """Validate required contact fields and create a person record."""
    payload = get_payload()
    require_fields(payload, ("name",))
    dto = parse_dto(PersonCreateDTO, payload)

    person = create_person(
        **dto.model_dump(),
    )
    audit_change("created", "person", person["id"], person.keys())
    return jsonify(person), 201


@blueprint.post("/people/import")
@roles_required(Role.ADMIN, Role.TREASURER)
def people_import():
    """Queue a bounded background people import."""
    return queue_csv_import("people", _people_import_now)


def _people_import_now():
    """Validate an entire people CSV before atomically creating its records."""
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

    allowed_fields = {"name", "email", "phone", "role", "active", "notes"}
    if not headers or "name" not in headers:
        return jsonify({"error": "The CSV must include a 'name' column. Name is mandatory."}), 400
    if len(headers) != len(set(headers)):
        return jsonify({"error": "The CSV contains duplicate column headings."}), 400
    unknown_fields = sorted(set(headers) - allowed_fields)
    if unknown_fields:
        return jsonify({"error": f"Unknown CSV column(s): {', '.join(unknown_fields)}."}), 400

    people = []
    errors = []
    allowed_roles = {"volunteer", "parent", "coach", "other"}
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
            role = (row.get("role") or "volunteer").strip().lower()
            if role not in allowed_roles:
                errors.append(
                    f"Row {row_number} has an invalid role; use volunteer, parent, coach, or other."
                )
                continue
            try:
                person = parse_dto(
                    PersonCreateDTO,
                    {
                        "name": as_text(row.get("name"), "name", maximum=200, required=True),
                        "email": as_email(row.get("email")),
                        "phone": as_text(row.get("phone"), "phone", maximum=50),
                        "role": role,
                        "active": as_bool((row.get("active") or "").strip(), default=True),
                        "notes": as_text(row.get("notes"), "notes", maximum=2000),
                    },
                ).model_dump()
            except ValueError as exc:
                errors.append(f"Row {row_number}: {exc}")
                continue
            people.append(person)
    except (csv.Error, ValueError) as exc:
        return jsonify({"error": str(exc) or "The CSV file is malformed."}), 400

    if errors:
        return jsonify({"error": errors[0], "errors": errors}), 400
    if not people:
        return jsonify({"error": "The CSV does not contain any people to import."}), 400
    if csv_import_is_preview():
        return jsonify({"preview": True, "valid_rows": len(people)})
    with csv_import_writes():
        created = create_people(people)
        for person in created:
            audit_change("created", "person", person["id"], person.keys())
    return jsonify({"imported": len(created), "items": created}), 201


@blueprint.get("/people/import/template")
@roles_required(Role.ADMIN, Role.TREASURER)
def people_import_template():
    """Download the CSV header accepted by the people import."""
    return Response(
        "name,email,phone,role,active,notes\r\n",
        mimetype="text/csv",
        headers={"Content-Disposition": 'attachment; filename="people-template.csv"'},
    )


@blueprint.get("/people/<int:person_id>")
@roles_required(Role.ADMIN, Role.TREASURER, Role.VOLUNTEER)
def people_get(person_id: int):
    """Fetch one person, applying the volunteer's own-record restriction."""
    if not can_view_person(g.current_user, person_id):
        return jsonify({"error": "Person not found"}), 404
    person = get_person(person_id)
    if person is None:
        return jsonify({"error": "Person not found"}), 404
    return jsonify(person)


@blueprint.get("/people/<int:person_id>/export")
@roles_required(Role.ADMIN)
def people_export(person_id: int):
    """Return one person's records for administrator-led DSAR review."""
    person = get_person(person_id)
    if person is None:
        return jsonify({"error": "Person not found"}), 404
    return jsonify(
        {
            "person": person,
            "trips": list_trips(person_id),
            "declarations": list_declarations(person_id),
            "donations": list_donations(person_id),
            "payments": list_payments(person_id),
        }
    )


@blueprint.put("/people/<int:person_id>")
@roles_required(Role.ADMIN, Role.TREASURER)
def people_update(person_id: int):
    """Edit a person and audit the changed field names."""
    payload = get_payload()
    dto = parse_dto(PersonUpdateDTO, payload)
    payload = dto.model_dump(exclude_unset=True)
    if "active" in payload:
        payload["active"] = as_bool(payload["active"])
    if "name" in payload:
        payload["name"] = as_text(payload["name"], "name", maximum=200, required=True)
    if "email" in payload:
        payload["email"] = as_email(payload["email"])
    if "phone" in payload:
        payload["phone"] = as_text(payload["phone"], "phone", maximum=50)
    if "notes" in payload:
        payload["notes"] = as_text(payload["notes"], "notes", maximum=2000)
    person = update_person(person_id, **payload)
    if person is None:
        return jsonify({"error": "Person not found"}), 404
    action = (
        "dormant"
        if payload.get("active") is False
        else "reactivated"
        if payload.get("active") is True
        else "updated"
    )
    audit_change(action, "person", person_id, payload.keys())
    return jsonify(person)


@blueprint.delete("/people/<int:person_id>")
@roles_required(Role.ADMIN, Role.TREASURER)
def people_delete(person_id: int):
    """Delete unused people and make historically referenced people dormant."""
    outcome = delete_or_deactivate_person(person_id)
    if outcome is None:
        return jsonify({"error": "Person not found"}), 404
    audit_change(outcome, "person", person_id, ("active",))
    return jsonify({outcome: True, "outcome": outcome})
