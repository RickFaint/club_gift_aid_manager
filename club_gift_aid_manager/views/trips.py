"""Trips routes.

A Blueprint is Flask's way of collecting related URL handlers.  Defining a
route here does not start a second application: create_app() later registers
this collection on the one Flask application object.
"""

import logging

from flask import Blueprint

from .shared import (
    HTML_FORM,
    MAX_IMPORT_ROWS,
    Response,
    Role,
    RouteLookupError,
    audit_change,
    calculate_route_distance,
    calculate_trip_amounts,
    csv,
    csv_import_is_preview,
    csv_import_writes,
    date,
    hashlib,
    g,
    get_declaration_text,
    get_pagination,
    get_payload,
    get_person,
    get_search_query,
    get_rate_schedule_for_date,
    get_trip,
    io,
    jsonify,
    list_destinations,
    list_people,
    list_trips,
    paginated_response,
    queue_csv_import,
    rate_schedule_to_bands,
    render_template,
    render_template_string,
    request,
    secrets,
    sqlite3,
    validate_declaration,
)
from ..auth.decorators import roles_required
from ..core.validation import (
    as_bool,
    as_date,
    as_int,
    as_non_negative_float,
    as_non_negative_int,
    require_fields,
)
from ..dtos import TripCreateDTO, TripCSVRowDTO
from ..dtos.trips import PaymentQuoteDTO, TripCorrectionDTO
from ..db import (
    create_trip_correction,
    get_payment_calculation,
    get_cached_route_miles,
    get_trip_import_locations,
    get_trip_import_people,
    list_trip_corrections,
    quote_trip_payment,
    save_cached_route_miles,
)
from ..domain.policies import can_assign_imported_trips, trip_scope_person_id
from ..core.validation import parse_dto
from ..services.workflows import TripService, WorkflowConflictError, WorkflowNotFoundError
from ..core.logging import log_event


blueprint = Blueprint("trips", __name__)
trip_service = TripService()
logger = logging.getLogger(__name__)


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
        idempotency_key=secrets.token_urlsafe(24),
    )


@blueprint.post("/claim")
@roles_required(Role.ADMIN, Role.TREASURER, Role.VOLUNTEER)
def submit_claim():
    """Validate a submitted claim, enforce volunteer ownership, and save it."""
    payload = get_payload()
    payload.pop("_csrf_token", None)
    payload.setdefault("vehicle_type", "car")
    response, status_code = _create_trip(payload)
    if status_code == 201:
        return jsonify({"status": "ok", "trip": response.get_json()}), 201
    return response, status_code


@blueprint.get("/api/trip/<int:trip_id>")
@roles_required(Role.ADMIN, Role.TREASURER, Role.VOLUNTEER)
def get_trip_detail(trip_id: int):
    """Return one trip while hiding another volunteer's trip as not found."""
    try:
        trip = trip_service.get(trip_id, actor=g.current_user)
    except WorkflowNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
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


@blueprint.post("/api/route-distance")
@roles_required(Role.ADMIN, Role.TREASURER, Role.VOLUNTEER)
def route_distance():
    """Calculate road miles and cache coordinates via a CSRF-protected write."""
    payload = get_payload()
    try:
        origin_id = int(payload.get("origin_id", 0))
        destination_id = int(payload.get("destination_id", 0))
    except (TypeError, ValueError):
        origin_id = destination_id = 0
    if not all(0 < value <= 2**63 - 1 for value in (origin_id, destination_id)):
        return jsonify({"error": "Select both an origin and a destination."}), 400
    try:
        return jsonify(calculate_route_distance(origin_id, destination_id))
    except RouteLookupError as exc:
        log_event(
            logger,
            "routing_service_unavailable",
            level=logging.WARNING,
            request_id=getattr(g, "request_id", None),
            status_code=exc.status_code,
            outcome="lookup_failed",
        )
        return jsonify({"error": str(exc)}), exc.status_code


@blueprint.get("/trips")
@roles_required(Role.ADMIN, Role.TREASURER, Role.VOLUNTEER)
def trips_index():
    """List trips, scoped to the logged-in person for volunteer accounts."""
    # Passing a person ID scopes SQL results before they leave the database layer.
    person_id = trip_scope_person_id(g.current_user)
    if person_id is None:
        person_id = request.args.get("person_id", type=int)
    unpaid_only = request.args.get("unpaid", "").lower() in {"1", "true", "yes"}
    limit, offset = get_pagination()
    items, total = list_trips(
        person_id,
        limit=limit,
        offset=offset,
        with_total=True,
        unpaid_only=unpaid_only,
        search=get_search_query(),
    )
    return jsonify(paginated_response(items, total, limit, offset))


@blueprint.post("/trips")
@roles_required(Role.ADMIN, Role.TREASURER, Role.VOLUNTEER)
def trips_create():
    """Create a trip for the selected person only when the caller is an admin."""
    return _create_trip(get_payload())


def _create_trip(payload):
    """Use the same validated, server-calculated mileage for either claim form."""
    idempotency_key = request.headers.get("Idempotency-Key") or payload.pop("_idempotency_key", "")
    request_token = (
        hashlib.sha256(f"{g.current_user['id']}:{idempotency_key}".encode()).hexdigest()
        if 16 <= len(idempotency_key) <= 200 else None
    )
    required = ["trip_date", "vehicle_type", "origin_id", "destination_id"]
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
        if not 0 < person_id <= 2**63 - 1 or get_person(person_id) is None:
            return jsonify({"error": "Person not found."}), 404

    values = dict(payload)
    # Older clients send a distance for display. Only the saved locations establish mileage.
    values.pop("miles_one_way", None)
    values.update(person_id=person_id, declaration_accepted=declaration_accepted)
    if values.get("passengers") in (None, ""):
        values.pop("passengers", None)
    dto = parse_dto(TripCreateDTO, values)
    try:
        distance = _saved_route_miles(dto.origin_id, dto.destination_id)
        trip = trip_service.create(
            dto.model_dump() | {"miles_one_way": distance, "request_token": request_token},
            actor=g.current_user,
            declaration_text=declaration_text,
        )
    except RouteLookupError as exc:
        return jsonify({"error": str(exc)}), exc.status_code
    except WorkflowNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except PermissionError as exc:
        return jsonify({"error": str(exc)}), 403
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(trip), 201


def _saved_route_miles(origin_id: int, destination_id: int) -> float:
    """Normalize the distance returned by the configured routing service."""
    cached = get_cached_route_miles(origin_id, destination_id)
    if cached is not None:
        return cached
    distance = as_non_negative_float(
        calculate_route_distance(origin_id, destination_id)["distance_miles"], "route distance"
    )
    save_cached_route_miles(origin_id, destination_id, distance)
    return distance


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
        return jsonify(
            {
                "error": "The CSV file could not be read. Use UTF-8 encoding and valid CSV formatting."
            }
        ), 400

    allowed_fields = {
        "person_id",
        "person_name",
        "origin_id",
        "destination_id",
        "trip_date",
        "vehicle_type",
        "miles_one_way",
        "return_trip",
        "passengers",
        "declaration_accepted",
    }
    if not headers:
        return jsonify({"error": "The CSV must include the required trip columns."}), 400
    if len(headers) != len(set(headers)):
        return jsonify({"error": "The CSV contains duplicate column headings."}), 400
    unknown_fields = sorted(set(headers) - allowed_fields)
    if unknown_fields:
        return jsonify({"error": f"Unknown CSV column(s): {', '.join(unknown_fields)}."}), 400
    if not {
        "origin_id",
        "destination_id",
        "trip_date",
        "vehicle_type",
        "declaration_accepted",
    } <= set(headers):
        return jsonify(
            {
                "error": "Required columns are origin_id, destination_id, trip_date, vehicle_type, and declaration_accepted."
            }
        ), 400

    is_admin = can_assign_imported_trips(g.current_user)
    if is_admin and not ({"person_id", "person_name"} & set(headers)):
        return jsonify(
            {"error": "Admin uploads must include a person_id or person_name column."}
        ), 400
    if is_admin and {"person_id", "person_name"} <= set(headers):
        return jsonify({"error": "Use either person_id or person_name, not both."}), 400
    if not is_admin and ({"person_id", "person_name"} & set(headers)):
        return jsonify(
            {
                "error": "Only administrators can assign trips to other people; remove the person_id/person_name column."
            }
        ), 403
    if not is_admin and not g.current_user["person_id"]:
        return jsonify(
            {"error": "This account is not linked to a person and cannot create trips."}
        ), 403

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
                return jsonify(
                    {
                        "error": (
                            f"The CSV contains more than {MAX_IMPORT_ROWS} data rows. "
                            "Split it into smaller files."
                        )
                    }
                ), 400
            if None in row:
                errors.append(f"Row {row_number}: has more values than the CSV headings.")
                continue
            if not any(value and value.strip() for value in row.values() if isinstance(value, str)):
                continue

            required_fields = (
                "origin_id",
                "destination_id",
                "trip_date",
                "vehicle_type",
                "declaration_accepted",
            )
            missing = [field for field in required_fields if not (row.get(field) or "").strip()]
            if missing:
                errors.append(
                    f"Row {row_number}: is missing required field(s): {', '.join(missing)}."
                )
                continue

            try:
                if is_admin:
                    if "person_id" in headers:
                        try:
                            person_id = int((row.get("person_id") or "").strip())
                        except ValueError as exc:
                            raise ValueError(
                                f"Row {row_number}: must have a valid person_id."
                            ) from exc
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
                if not all(0 < value <= 2**63 - 1 for value in (person_id, origin_id, destination_id)):
                    raise ValueError("Invalid person or location ID.")
            except (TypeError, ValueError) as exc:
                message = str(exc)
                if not message.startswith(f"Row {row_number}:"):
                    message = f"Row {row_number}: has an invalid person, date, location ID, or passenger count."
                errors.append(message)
                continue
            vehicle_type = (row.get("vehicle_type") or "").strip().lower()
            if vehicle_type not in {"car", "motorcycle", "bicycle"}:
                errors.append(
                    f"Row {row_number}: has an invalid vehicle_type; use car, motorcycle, or bicycle."
                )
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
                    errors.append(
                        f"Row {row_number}: has an invalid declaration_accepted value; use yes."
                    )
                continue

            parsed = parse_dto(
                TripCSVRowDTO,
                {
                    "person_id": person_id,
                    "origin_id": origin_id,
                    "destination_id": destination_id,
                    "trip_date": trip_date,
                    "vehicle_type": vehicle_type,
                    "return_trip": return_value in truthy_values,
                    "passengers": passengers,
                    "declaration_accepted": True,
                },
            ).model_dump()
            parsed_rows.append((row_number, parsed))
    except csv.Error as exc:
        errors.append(f"CSV file is malformed: {exc}")

    person_ids = {trip["person_id"] for _, trip in parsed_rows}
    location_ids = {
        trip[field] for _, trip in parsed_rows for field in ("origin_id", "destination_id")
    }
    existing_people = get_trip_import_people(person_ids)
    active_locations = get_trip_import_locations(location_ids)
    for row_number, trip in parsed_rows:
        if trip["person_id"] not in existing_people:
            errors.append(f"Row {row_number}: references a person_id that does not exist.")
        for field in ("origin_id", "destination_id"):
            if trip[field] not in active_locations:
                errors.append(f"Row {row_number}: {field} was not found or is inactive.")
    if errors:
        return jsonify(
            {
                "error": "No trips were imported. Correct the listed CSV errors and try again.",
                "errors": errors,
            }
        ), 400

    route_cache: dict[tuple[int, int], float | str] = {}
    route_error_status = 400
    trips = []
    for row_number, trip in parsed_rows:
        route_key = (trip["origin_id"], trip["destination_id"])
        if route_key not in route_cache:
            try:
                route_cache[route_key] = _saved_route_miles(*route_key)
            except RouteLookupError as exc:
                log_event(
                    logger,
                    "routing_service_unavailable",
                    level=logging.WARNING,
                    request_id=getattr(g, "request_id", None),
                    status_code=exc.status_code,
                    outcome="csv_lookup_failed",
                )
                route_cache[route_key] = str(exc)
                route_error_status = max(route_error_status, exc.status_code)
            except (KeyError, TypeError, ValueError) as exc:
                route_cache[route_key] = str(exc) or "Unable to calculate the road distance."
        route_result = route_cache[route_key]
        if isinstance(route_result, str):
            errors.append(
                f"Row {row_number}: road distance could not be calculated: {route_result}"
            )
            continue
        trips.append({**trip, "miles_one_way": route_result})

    if not trips:
        if errors:
            return jsonify(
                {
                    "error": "No trips were imported. Correct the listed CSV errors and try again.",
                    "errors": errors,
                }
            ), route_error_status
        return jsonify({"error": "The CSV does not contain any trips to import."}), 400

    if errors:
        return jsonify(
            {
                "error": "No trips were imported. Correct the listed CSV errors and try again.",
                "errors": errors,
            }
        ), route_error_status

    if csv_import_is_preview():
        return jsonify({"preview": True, "valid_rows": len(trips)}), 200

    declaration_text = get_declaration_text()
    try:
        with csv_import_writes():
            created = trip_service.create_many(
                trips,
                actor=g.current_user,
                declaration_text=declaration_text,
            )
    except ValueError as exc:
        message = str(exc)
        return jsonify({"error": "No trips were imported.", "errors": [message]}), 400
    except sqlite3.IntegrityError:
        message = "A trip references an invalid record; no trips were imported."
        return jsonify({"error": "No trips were imported.", "errors": [message]}), 400
    return jsonify({"imported": len(created), "items": created}), 201


@blueprint.get("/trips/import/template")
@roles_required(Role.ADMIN, Role.TREASURER, Role.VOLUNTEER)
def trips_import_template():
    """Download trip CSV headers appropriate to the current user's role."""
    headers = [
        "origin_id",
        "destination_id",
        "trip_date",
        "vehicle_type",
        "return_trip",
        "passengers",
        "declaration_accepted",
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
    try:
        trip = trip_service.get(trip_id, actor=g.current_user)
    except WorkflowNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    return jsonify(trip)


@blueprint.post("/payments/quote")
@roles_required(Role.ADMIN, Role.TREASURER)
def payment_quote():
    """Quote a full reimbursement from stored trip IDs in one request."""
    dto = parse_dto(PaymentQuoteDTO, get_payload())
    try:
        return jsonify(quote_trip_payment(dto.person_id, dto.trip_ids))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@blueprint.get("/payments/<int:payment_id>/calculation")
@roles_required(Role.ADMIN, Role.TREASURER)
def payment_calculation(payment_id: int):
    """Show the calculation recorded at payment time when one is available."""
    evidence = get_payment_calculation(payment_id)
    if evidence is None:
        return jsonify({"error": "Payment not found."}), 404
    return jsonify(evidence)


@blueprint.get("/trips/<int:trip_id>/corrections")
@roles_required(Role.ADMIN, Role.TREASURER)
def trip_corrections(trip_id: int):
    """Show the separate correction evidence to authorized staff."""
    if get_trip(trip_id) is None:
        return jsonify({"error": "Trip not found."}), 404
    return jsonify({"items": list_trip_corrections(trip_id)})


@blueprint.post("/trips/<int:trip_id>/corrections")
@roles_required(Role.ADMIN, Role.TREASURER)
def trip_correction_create(trip_id: int):
    """Record and audit a correction without changing a paid trip or its payment."""
    dto = parse_dto(TripCorrectionDTO, get_payload())
    try:
        correction = create_trip_correction(
            trip_id, author_user_id=g.current_user["id"], note=dto.note
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409
    if correction is None:
        return jsonify({"error": "Trip not found."}), 404
    audit_change("correction_recorded", "trip", trip_id, ("correction_note",))
    return jsonify(correction), 201


@blueprint.put("/trips/<int:trip_id>")
@roles_required(Role.ADMIN, Role.TREASURER)
def trips_update(trip_id: int):
    """Update an administrative trip record and note fields changed in the audit."""
    payload = get_payload()
    existing = get_trip(trip_id)
    if existing is None:
        return jsonify({"error": "Trip not found."}), 404
    if existing["payment_id"]:
        return jsonify({"error": "A paid trip is locked. Record a correction note instead."}), 409
    unsupported = set(payload) - {
        "person_id", "origin_id", "destination_id", "vehicle_type", "trip_date", "miles_one_way",
        "return_trip", "passengers", "status",
    }
    if unsupported:
        return jsonify({"error": "The request contains fields that cannot be edited."}), 400
    if "vehicle_type" in payload and payload["vehicle_type"] not in ("car", "motorcycle", "bicycle"):
        return jsonify({"error": "Vehicle type must be car, motorcycle, or bicycle."}), 400
    if "status" in payload and payload["status"] not in ("submitted", "approved", "rejected"):
        return jsonify({"error": "Status must be submitted, approved, or rejected."}), 400
    if "person_id" in payload:
        person_id = as_int(payload["person_id"], "person_id")
        if not 0 < person_id <= 2**63 - 1 or get_person(person_id) is None:
            return jsonify({"error": "Person not found."}), 404
        payload["person_id"] = person_id
    if "passengers" in payload:
        payload["passengers"] = as_non_negative_int(payload["passengers"], "passengers")
    if "trip_date" in payload:
        payload["trip_date"] = as_date(payload["trip_date"], "trip_date")
    if "return_trip" in payload:
        payload["return_trip"] = as_bool(payload["return_trip"])
    try:
        if {"origin_id", "destination_id", "miles_one_way"} & payload.keys():
            origin_id = as_int(payload.get("origin_id", existing["origin_id"]), "origin_id")
            destination_id = as_int(
                payload.get("destination_id", existing["destination_id"]), "destination_id"
            )
            if not all(0 < value <= 2**63 - 1 for value in (origin_id, destination_id)):
                return jsonify({"error": "Select two valid locations."}), 400
            payload.update(
                origin_id=origin_id,
                destination_id=destination_id,
                miles_one_way=_saved_route_miles(origin_id, destination_id),
            )
        trip = trip_service.update(trip_id, payload, actor=g.current_user)
    except RouteLookupError as exc:
        return jsonify({"error": str(exc)}), exc.status_code
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except WorkflowNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    return jsonify(trip)


@blueprint.delete("/trips/<int:trip_id>")
@roles_required(Role.ADMIN, Role.TREASURER)
def trips_delete(trip_id: int):
    """Delete an unpaid trip; the data layer rejects paid trips."""
    try:
        trip_service.delete(trip_id, actor=g.current_user)
    except WorkflowConflictError as exc:
        return jsonify({"error": str(exc)}), 409
    except WorkflowNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    return jsonify({"deleted": True})
