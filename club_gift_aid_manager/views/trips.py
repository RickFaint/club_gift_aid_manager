"""Trips routes.

A Blueprint is Flask's way of collecting related URL handlers.  Defining a
route here does not start a second application: create_app() later registers
this collection on the one Flask application object.
"""

from flask import Blueprint

from .shared import *  # noqa: F403 - shared view vocabulary is intentionally centralised
from ..auth.decorators import roles_required
from ..validation import (
    as_bool, as_date, as_int, as_non_negative_float, as_non_negative_int, require_fields,
)


blueprint = Blueprint("trips", __name__)

@blueprint.get("/trips/page")
@roles_required(Role.ADMIN, Role.TREASURER, Role.VOLUNTEER)
def trips_page():
    """Render the trip form with the exact declaration text to be accepted."""
    return render_template("trips.html", declaration_text=get_declaration_text())


@blueprint.get("/claim")
def claim_page():
    """Render the lightweight claim form with active locations and home default."""
    return render_template_string(
        HTML_FORM,
        declaration_text=get_declaration_text(),
        locations=list_destinations(active_only=True),
    )


@blueprint.post("/claim")
@roles_required(Role.ADMIN, Role.TREASURER, Role.VOLUNTEER)
def submit_claim():
    """Validate a submitted claim, enforce volunteer ownership, and save it."""
    payload = get_payload()
    try:
        trip_date = date.fromisoformat(payload["trip_date"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "Trip date is required and must be ISO format."}), 400

    vehicle_type = payload.get("vehicle_type", "car")
    miles_one_way = as_non_negative_float(payload.get("miles_one_way", 0) or 0, "miles_one_way")
    return_trip = as_bool(payload.get("return_trip"))
    passengers = as_non_negative_int(payload.get("passengers", 0) or 0, "passengers")
    declaration_accepted = as_bool(payload.get("declaration_accepted"))
    declaration_text = get_declaration_text()

    try:
        validate_declaration(declaration_accepted, declaration_text)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    # The request cannot choose a different owner for a volunteer's claim.
    person_id = g.current_user["person_id"] if g.current_user["role"] == Role.VOLUNTEER.value else int(payload.get("person_id", 0))
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


@blueprint.get("/api/trip/<int:trip_id>")
@roles_required(Role.ADMIN, Role.TREASURER, Role.VOLUNTEER)
def get_trip_detail(trip_id: int):
    """Return one trip while hiding another volunteer's trip as not found."""
    trip = get_trip(trip_id)
    if trip is None:
        return jsonify({"error": "Trip not found"}), 404
    if g.current_user["role"] == Role.VOLUNTEER.value and trip["person_id"] != g.current_user["person_id"]:
        return jsonify({"error": "Trip not found"}), 404
    return jsonify(trip)


@blueprint.get("/api/calculate")
@roles_required(Role.ADMIN, Role.TREASURER, Role.VOLUNTEER)
def calculate_route():
    """Return a reimbursement breakdown from trip details supplied by the UI."""
    payload = request.args.to_dict()
    try:
        trip_date = date.fromisoformat(payload["trip_date"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "Trip date is required and must be ISO format."}), 400

    miles_one_way = as_non_negative_float(payload.get("miles_one_way", 0) or 0, "miles_one_way")
    round_trip = as_bool(payload.get("return_trip"))
    passengers = as_non_negative_int(payload.get("passengers", 0) or 0, "passengers")
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


@blueprint.get("/api/route-distance")
@roles_required(Role.ADMIN, Role.TREASURER, Role.VOLUNTEER)
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


@blueprint.get("/trips")
@roles_required(Role.ADMIN, Role.TREASURER, Role.VOLUNTEER)
def trips_index():
    """List trips, scoped to the logged-in person for volunteer accounts."""
    # Passing a person ID scopes SQL results before they leave the database layer.
    person_id = g.current_user["person_id"] if g.current_user["role"] == Role.VOLUNTEER.value else None
    limit, offset = get_pagination()
    items, total = list_trips(
        person_id, limit=limit, offset=offset, with_total=True
    )
    return jsonify(paginated_response(items, total, limit, offset))


@blueprint.post("/trips")
@roles_required(Role.ADMIN, Role.TREASURER, Role.VOLUNTEER)
def trips_create():
    """Create a trip for the selected person only when the caller is an admin."""
    payload = get_payload()
    required = ["trip_date", "vehicle_type", "miles_one_way", "origin_id", "destination_id"]
    if g.current_user["role"] == Role.ADMIN.value:
        required.append("person_id")
    require_fields(payload, required)

    declaration_text = get_declaration_text()
    declaration_accepted = as_bool(payload.get("declaration_accepted"))
    try:
        validate_declaration(declaration_accepted, declaration_text)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    # Non-admin accounts can only create trips for their linked person.
    if g.current_user["role"] != Role.ADMIN.value:
        person_id = g.current_user["person_id"]
        if not person_id:
            return jsonify({"error": "This account is not linked to a person."}), 403
    else:
        person_id = as_int(payload["person_id"], "person_id")
        if get_person(person_id) is None:
            return jsonify({"error": "Person not found."}), 404

    try:
        trip = create_trip(
            person_id=person_id,
            origin_id=as_int(payload["origin_id"], "origin_id"),
            destination_id=as_int(payload["destination_id"], "destination_id"),
            vehicle_type=payload["vehicle_type"],
            trip_date=as_date(payload["trip_date"], "trip_date"),
            miles_one_way=as_non_negative_float(payload["miles_one_way"], "miles_one_way"),
            return_trip=as_bool(payload.get("return_trip")),
            passengers=as_non_negative_int(payload.get("passengers", 0) or 0, "passengers"),
            declaration_accepted=declaration_accepted,
            declaration_text=declaration_text,
            tax_year=calculate_tax_year(as_date(payload["trip_date"], "trip_date")),
            status="submitted",
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    audit_change("created", "trip", trip["id"], trip.keys())
    return jsonify(trip), 201


@blueprint.post("/trips/import")
@roles_required(Role.ADMIN, Role.TREASURER, Role.VOLUNTEER)
def trips_import():
    """Queue a bounded background trip import."""
    return queue_csv_import("trips", _trips_import_now)


def _trips_import_now():
    """Validate and atomically import trip records inside a worker context."""
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

    is_admin = g.current_user["role"] == Role.ADMIN.value
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
            if row_number > MAX_IMPORT_ROWS + 1:
                return jsonify({
                    "error": (
                        f"The CSV contains more than {MAX_IMPORT_ROWS} data rows. "
                        "Split it into smaller files."
                    )
                }), 400
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
                route_cache[route_key] = as_non_negative_float(
                    calculate_route_distance(*route_key)["distance_miles"],
                    "route distance",
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


@blueprint.get("/trips/import/template")
@roles_required(Role.ADMIN, Role.TREASURER, Role.VOLUNTEER)
def trips_import_template():
    """Download trip CSV headers appropriate to the current user's role."""
    headers = [
        "origin_id", "destination_id", "trip_date", "vehicle_type",
        "return_trip", "passengers", "declaration_accepted",
    ]
    if g.current_user["role"] == Role.ADMIN.value:
        headers.insert(0, "person_id")
    return Response(
        ",".join(headers) + "\r\n",
        mimetype="text/csv",
        headers={"Content-Disposition": 'attachment; filename="trips-template.csv"'},
    )


@blueprint.get("/trips/<int:trip_id>")
@roles_required(Role.ADMIN, Role.TREASURER, Role.VOLUNTEER)
def trips_get(trip_id: int):
    """Return one trip, hiding records owned by another volunteer."""
    trip = get_trip(trip_id)
    if trip is None:
        return jsonify({"error": "Trip not found"}), 404
    if g.current_user["role"] == Role.VOLUNTEER.value and trip["person_id"] != g.current_user["person_id"]:
        return jsonify({"error": "Trip not found"}), 404
    return jsonify(trip)


@blueprint.put("/trips/<int:trip_id>")
@roles_required(Role.ADMIN, Role.TREASURER)
def trips_update(trip_id: int):
    """Update an administrative trip record and note fields changed in the audit."""
    payload = get_payload()
    if "miles_one_way" in payload:
        payload["miles_one_way"] = as_non_negative_float(payload["miles_one_way"], "miles_one_way")
    if "passengers" in payload:
        payload["passengers"] = as_non_negative_int(payload["passengers"], "passengers")
    if "trip_date" in payload:
        payload["trip_date"] = as_date(payload["trip_date"], "trip_date")
    for field in ("return_trip", "declaration_accepted"):
        if field in payload:
            payload[field] = as_bool(payload[field])
    try:
        trip = update_trip(trip_id, **payload)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if trip is None:
        return jsonify({"error": "Trip not found"}), 404
    audit_change("updated", "trip", trip_id, payload.keys())
    return jsonify(trip)


@blueprint.delete("/trips/<int:trip_id>")
@roles_required(Role.ADMIN, Role.TREASURER)
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


