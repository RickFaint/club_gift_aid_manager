"""Destinations routes.

A Blueprint is Flask's way of collecting related URL handlers.  Defining a
route here does not start a second application: create_app() later registers
this collection on the one Flask application object.
"""

from flask import Blueprint

from ..db import list_destination_choices

from .shared import (
    MAX_IMPORT_ROWS,
    Response,
    Role,
    audit_change,
    audit_created_batch,
    create_destination,
    create_destinations,
    csv,
    csv_import_is_preview,
    csv_import_writes,
    delete_destination,
    g,
    get_pagination,
    get_payload,
    get_search_query,
    io,
    jsonify,
    list_destinations,
    paginated_response,
    queue_csv_import,
    render_template,
    request,
    sqlite3,
    update_destination,
)
from ..auth.decorators import roles_required
from ..db import destination_import_conflicts
from ..core.validation import as_bool, as_text, require_fields
from ..dtos import DestinationCreateDTO, DestinationCSVRowDTO, DestinationUpdateDTO
from ..core.validation import parse_dto


blueprint = Blueprint("destinations", __name__)


@blueprint.get("/destinations/page")
@roles_required(Role.ADMIN, Role.TREASURER)
def destinations_page():
    """Render the staff-only location masterfile."""
    return render_template("destinations.html")


@blueprint.get("/destinations")
@roles_required(Role.ADMIN, Role.TREASURER, Role.VOLUNTEER)
def destinations_index():
    """Return all locations to staff and only active trip choices to volunteers."""
    limit, offset = get_pagination()
    if g.current_user["role"] == Role.VOLUNTEER.value:
        choices = list_destination_choices()
        page = choices[offset : offset + limit]
        return jsonify(paginated_response(page, len(choices), limit, offset))
    items, total = list_destinations(
        limit=limit, offset=offset, with_total=True,
        search=get_search_query(),
    )
    return jsonify(paginated_response(items, total, limit, offset))


@blueprint.get("/destinations/choices")
@roles_required(Role.ADMIN, Role.TREASURER, Role.VOLUNTEER)
def destinations_choices():
    """Return only active location labels and default origin metadata."""
    return jsonify({"items": list_destination_choices()})


@blueprint.post("/destinations")
@roles_required(Role.ADMIN, Role.TREASURER)
def destinations_create():
    """Create a location, optionally making it the club's single home ground."""
    payload = get_payload()
    require_fields(payload, ("name",))
    dto = parse_dto(DestinationCreateDTO, payload)
    try:
        destination = create_destination(
            **dto.model_dump(),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except sqlite3.IntegrityError:
        return jsonify({"error": "A destination with that name already exists."}), 409
    audit_change("created", "destination", destination["id"], destination.keys())
    return jsonify(destination), 201


@blueprint.post("/destinations/import")
@roles_required(Role.ADMIN, Role.TREASURER)
def destinations_import():
    """Queue a bounded background location import."""
    return queue_csv_import("destinations", _destinations_import_now)


def _destinations_import_now():
    """Validate and atomically import locations inside a worker context."""
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
            {
                "error": "The CSV file could not be read. Use UTF-8 encoding and valid CSV formatting."
            }
        ), 400

    allowed_fields = {"name", "address", "town_city", "postcode", "notes", "home_ground"}
    if not headers or "name" not in headers:
        return jsonify(
            {"error": "The CSV must include a 'name' column. Location name is mandatory."}
        ), 400
    if len(headers) != len(set(headers)):
        return jsonify({"error": "The CSV contains duplicate column headings."}), 400
    unknown_fields = sorted(set(headers) - allowed_fields)
    if unknown_fields:
        return jsonify({"error": f"Unknown CSV column(s): {', '.join(unknown_fields)}."}), 400

    location_rows = []
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
            name = (row.get("name") or "").strip()
            if not name:
                errors.append(f"Row {row_number} is missing the mandatory name.")
                continue
            home_ground_value = (row.get("home_ground") or "").strip().lower()
            if home_ground_value not in {"", "true", "1", "yes", "on", "false", "0", "no", "off"}:
                errors.append(
                    f"Row {row_number} has an invalid home_ground value; use yes or no."
                )
                continue
            home_ground = home_ground_value in {"true", "1", "yes", "on"}
            limits = {"name": 200, "address": 500, "town_city": 200, "postcode": 20, "notes": 2000}
            try:
                raw_location = {
                    field: as_text(row.get(field), field, maximum=maximum, required=field == "name")
                    for field, maximum in limits.items()
                }
                location = parse_dto(
                    DestinationCSVRowDTO,
                    raw_location | {"home_ground": home_ground},
                ).model_dump()
            except ValueError as exc:
                errors.append(f"Row {row_number}: {exc}")
                continue
            location_rows.append((row_number, location | {"home_ground": home_ground}))
    except (csv.Error, ValueError) as exc:
        return jsonify({"error": str(exc) or "The CSV file is malformed."}), 400

    if errors:
        return jsonify({"error": errors[0], "errors": errors}), 400
    if not location_rows:
        return jsonify({"error": "The CSV does not contain any locations to import."}), 400

    locations = []
    home_ground_rows = []
    skipped_duplicate_rows = []
    seen_names = set()
    input_row_numbers = []
    ascii_lower = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
    for row_number, location in location_rows:
        name_key = location["name"].translate(ascii_lower)
        if name_key in seen_names:
            skipped_duplicate_rows.append(row_number)
            continue
        seen_names.add(name_key)
        locations.append(location)
        input_row_numbers.append(row_number)
        if location["home_ground"]:
            home_ground_rows.append(row_number)

    if len(home_ground_rows) > 1:
        return jsonify({
            "error": "Only one row may be marked as the club home ground.",
            "errors": [f"Row {number}: marked as home ground." for number in home_ground_rows],
        }), 400

    if csv_import_is_preview():
        conflicts = destination_import_conflicts(locations)
        if conflicts:
            errors = [
                f'Row {input_row_numbers[index]}: location name "{name}" conflicts.'
                for index, name in conflicts.items()
            ]
            return jsonify({"error": errors[0], "errors": errors}), 409
        return jsonify({
            "preview": True,
            "valid_rows": len(locations),
            "skipped_duplicate_rows": skipped_duplicate_rows,
        })

    try:
        with csv_import_writes():
            created = create_destinations(locations)
            audit_created_batch("destination", created)
    except ValueError as exc:
        return jsonify({"error": f"{exc} No locations were imported."}), 409
    except sqlite3.IntegrityError:
        return jsonify(
            {"error": "A location name already exists. No locations were imported."}
        ), 409
    return jsonify(
        {
            "imported": len(created),
            "items": created,
            "skipped_duplicate_rows": skipped_duplicate_rows,
        }
    ), 201


@blueprint.get("/destinations/import/template")
@roles_required(Role.ADMIN, Role.TREASURER)
def destinations_import_template():
    """Download the CSV header row accepted by the location import."""
    return Response(
        "name,address,town_city,postcode,notes,home_ground\r\n",
        mimetype="text/csv",
        headers={"Content-Disposition": 'attachment; filename="locations-template.csv"'},
    )


@blueprint.put("/destinations/<int:destination_id>")
@roles_required(Role.ADMIN, Role.TREASURER)
def destinations_update(destination_id: int):
    """Update a location and audit edits or active/dormant status changes."""
    payload = get_payload()
    dto = parse_dto(DestinationUpdateDTO, payload)
    payload = dto.model_dump(exclude_unset=True)
    if "active" in payload:
        payload["active"] = as_bool(payload["active"])
    if "home_ground" in payload:
        payload["home_ground"] = as_bool(payload["home_ground"])
    for field, maximum in {
        "name": 200,
        "address": 500,
        "town_city": 200,
        "postcode": 20,
        "notes": 2000,
    }.items():
        if field in payload:
            payload[field] = as_text(
                payload[field], field, maximum=maximum, required=field == "name"
            )
    try:
        destination = update_destination(destination_id, **payload)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except sqlite3.IntegrityError:
        return jsonify({"error": "A destination with that name already exists."}), 409
    if destination is None:
        return jsonify({"error": "Destination not found."}), 404
    audit_change(
        "dormant"
        if payload.get("active") is False
        else "reactivated"
        if payload.get("active") is True
        else "updated",
        "destination",
        destination_id,
        payload.keys(),
    )
    return jsonify(destination)


@blueprint.delete("/destinations/<int:destination_id>")
@roles_required(Role.ADMIN, Role.TREASURER)
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
