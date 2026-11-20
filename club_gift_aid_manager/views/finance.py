"""Finance routes.

A Blueprint is Flask's way of collecting related URL handlers.  Defining a
route here does not start a second application: create_app() later registers
this collection on the one Flask application object.
"""

import base64
import binascii

from flask import Blueprint, Response

from .shared import (
    Role,
    as_int,
    audit_change,
    create_declaration,
    deactivate_declaration,
    get_declaration,
    get_pagination,
    get_payload,
    get_person,
    get_privacy_notice_version,
    get_search_query,
    g,
    hmac,
    hashlib,
    jsonify,
    list_declarations,
    list_donations,
    list_payments,
    paginated_response,
    render_template,
    request,
    sqlite3,
    update_declaration,
)
from ..auth.decorators import roles_required
from ..core.validation import (
    as_bool,
    as_int,
    as_non_negative_int,
    as_optional_date,
    as_text,
    require_fields,
)
from ..dtos import DeclarationCreateDTO, DonationCreateDTO, PaymentCreateDTO
from ..core.validation import parse_dto
from ..services.workflows import DonationService, PaymentService, WorkflowNotFoundError


blueprint = Blueprint("finance", __name__)
donation_service = DonationService()
payment_service = PaymentService()


@blueprint.get("/declarations/page")
@roles_required(Role.ADMIN, Role.TREASURER)
def declarations_page():
    """Render the staff-only Gift Aid declaration page."""
    return render_template("declarations.html")


@blueprint.get("/donations/page")
@roles_required(Role.ADMIN, Role.TREASURER)
def donations_page():
    """Render donation management for finance roles."""
    return render_template("donations.html")


@blueprint.get("/payments/page")
@roles_required(Role.ADMIN, Role.TREASURER)
def payments_page():
    """Render the reimbursement and reconciliation page."""
    return render_template("payments.html")


@blueprint.get("/declarations")
@roles_required(Role.ADMIN, Role.TREASURER)
def declarations_index():
    """List Gift Aid declarations, optionally filtered to one person."""
    person_id = request.args.get("person_id", type=int)
    limit, offset = get_pagination()
    items, total = list_declarations(
        person_id, limit=limit, offset=offset, with_total=True, search=get_search_query()
    )
    return jsonify(paginated_response(items, total, limit, offset))


@blueprint.post("/declarations")
@roles_required(Role.ADMIN, Role.TREASURER)
def declarations_create():
    """Create a donor declaration and record its creation in the audit log."""
    payload = get_payload()
    require_fields(payload, ("person_id", "full_name"))
    dto = parse_dto(DeclarationCreateDTO, payload)
    if dto.evidence_image_base64:
        if dto.evidence_image_mime not in {"image/png", "image/jpeg", "image/webp"}:
            return jsonify({"error": "Use a PNG, JPEG, or WebP declaration image."}), 400
        try:
            decoded_image = base64.b64decode(dto.evidence_image_base64, validate=True)
        except (ValueError, binascii.Error):
            return jsonify({"error": "The declaration image is not valid base64."}), 400
        if len(decoded_image) > 512 * 1024:
            return jsonify({"error": "The declaration image must be 512 KiB or smaller."}), 400

    notice_version_id = payload.get("privacy_notice_version_id")
    if notice_version_id not in (None, ""):
        try:
            shown_notice = get_privacy_notice_version(
                as_int(notice_version_id, "privacy_notice_version_id")
            )
        except (TypeError, ValueError):
            shown_notice = None
        if shown_notice is None or not hmac.compare_digest(
            shown_notice["text_sha256"],
            str(payload.get("privacy_notice_sha256") or ""),
        ):
            return jsonify(
                {
                    "error": "The referenced privacy notice version is invalid. Refresh the page and try again."
                }
            ), 400
        notice_version_id = shown_notice["id"]
    else:
        notice_version_id = None

    declaration = create_declaration(
        person_id=dto.person_id,
        full_name=dto.full_name,
        home_address=dto.home_address,
        postcode=dto.postcode,
        declaration_date=dto.declaration_date.isoformat() if dto.declaration_date else None,
        scope=dto.scope,
        withdrawn_date=dto.withdrawn_date.isoformat() if dto.withdrawn_date else None,
        evidence_ref=dto.evidence_ref,
        evidence_image_base64=dto.evidence_image_base64,
        evidence_image_mime=dto.evidence_image_mime,
        privacy_notice_version_id=notice_version_id,
    )
    audit_change("created", "declaration", declaration["id"], declaration.keys())
    return jsonify(declaration), 201


@blueprint.get("/declarations/<int:declaration_id>")
@roles_required(Role.ADMIN, Role.TREASURER)
def declarations_get(declaration_id: int):
    """Fetch one declaration, including inactive records for staff review."""
    declaration = get_declaration(declaration_id)
    if declaration is None:
        return jsonify({"error": "Declaration not found."}), 404
    return jsonify(declaration)


@blueprint.get("/declarations/<int:declaration_id>/evidence-image")
@roles_required(Role.ADMIN, Role.TREASURER)
def declarations_evidence_image(declaration_id: int):
    """Decode and display stored declaration image evidence to authorised staff."""
    from ..database import unit_of_work
    from ..models import GiftAidDeclaration

    with unit_of_work() as database:
        declaration = database.get(GiftAidDeclaration, declaration_id)
        if declaration is None or not declaration.evidence_image_base64:
            return jsonify({"error": "Declaration image not found."}), 404
        if declaration.evidence_image_mime not in {"image/png", "image/jpeg", "image/webp"}:
            return jsonify({"error": "Stored declaration image type is invalid."}), 422
        try:
            image = base64.b64decode(declaration.evidence_image_base64, validate=True)
        except (ValueError, binascii.Error):
            return jsonify({"error": "Stored declaration image is invalid."}), 422
        return Response(image, mimetype=declaration.evidence_image_mime)


@blueprint.put("/declarations/<int:declaration_id>")
@roles_required(Role.ADMIN, Role.TREASURER)
def declarations_update(declaration_id: int):
    """Edit a declaration or restore it, then record the changed field names."""
    payload = get_payload()
    if "active" in payload:
        payload["active"] = as_bool(payload["active"])
    for field in ("declaration_date", "withdrawn_date"):
        if field in payload:
            payload[field] = as_optional_date(payload[field], field)
    for field, maximum in {
        "full_name": 200,
        "home_address": 500,
        "postcode": 20,
        "evidence_ref": 500,
    }.items():
        if field in payload:
            payload[field] = as_text(
                payload[field], field, maximum=maximum, required=field == "full_name"
            )
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
        "dormant"
        if payload.get("active") is False
        else "reactivated"
        if payload.get("active") is True
        else "updated"
    )
    audit_change(action, "declaration", declaration_id, payload.keys())
    return jsonify(declaration)


@blueprint.delete("/declarations/<int:declaration_id>")
@roles_required(Role.ADMIN, Role.TREASURER)
def declarations_delete(declaration_id: int):
    """Logically delete a declaration while preserving its historical evidence."""
    if not deactivate_declaration(declaration_id):
        return jsonify({"error": "Declaration not found."}), 404
    audit_change("dormant", "declaration", declaration_id, ("active",))
    return jsonify({"deleted": True, "active": False})


@blueprint.get("/donations")
@roles_required(Role.ADMIN, Role.TREASURER)
def donations_index():
    """List donations, including dormant items for administrative review."""
    person_id = request.args.get("person_id", type=int)
    limit, offset = get_pagination()
    items, total = list_donations(
        person_id, limit=limit, offset=offset, with_total=True, search=get_search_query()
    )
    return jsonify(paginated_response(items, total, limit, offset))


@blueprint.post("/donations")
@roles_required(Role.ADMIN, Role.TREASURER)
def donations_create():
    """Create a donation after validating its person and optional associations."""
    payload = get_payload()
    require_fields(payload, ("person_id", "amount_pence"))
    dto = parse_dto(DonationCreateDTO, payload)

    try:
        donation = donation_service.create(
            dto.model_dump(mode="json"),
            actor=g.current_user,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(donation), 201


@blueprint.put("/donations/<int:donation_id>")
@roles_required(Role.ADMIN, Role.TREASURER)
def donations_update(donation_id: int):
    """Edit a donation or toggle its logical-active flag, then audit the change."""
    payload = get_payload()
    if "active" in payload:
        payload["active"] = as_bool(payload["active"])
    for field in ("gift_aid_eligible",):
        if field in payload:
            payload[field] = as_bool(payload[field])
    if "amount_pence" in payload:
        payload["amount_pence"] = as_non_negative_int(payload["amount_pence"], "amount_pence")
    for field in ("donated_date", "gift_aid_claimed_date", "received_date"):
        if field in payload:
            payload[field] = as_optional_date(payload[field], field)
    for field, maximum in {"method": 100, "reference": 500, "bank_reference": 500}.items():
        if field in payload:
            payload[field] = as_text(payload[field], field, maximum=maximum)
    try:
        donation = donation_service.update(donation_id, payload, actor=g.current_user)
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    except WorkflowNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    return jsonify(donation)


@blueprint.get("/payments")
@roles_required(Role.ADMIN, Role.TREASURER)
def payments_index():
    """List reimbursements with their linked trips and donation totals."""
    person_id = request.args.get("person_id", type=int)
    limit, offset = get_pagination()
    items, total = list_payments(
        person_id, limit=limit, offset=offset, with_total=True, search=get_search_query()
    )
    return jsonify(paginated_response(items, total, limit, offset))


@blueprint.post("/payments")
@roles_required(Role.ADMIN, Role.TREASURER)
def payments_create():
    """Create a payment and optionally link multiple fully-covered trips."""
    payload = get_payload()
    require_fields(payload, ("person_id", "amount_pence"))
    dto = parse_dto(PaymentCreateDTO, payload)
    idempotency_key = request.headers.get("Idempotency-Key", "")
    request_token = (
        hashlib.sha256(f"{g.current_user['id']}:{idempotency_key}".encode()).hexdigest()
        if 16 <= len(idempotency_key) <= 200 else None
    )

    try:
        # Multi-select form values are serialized as a JSON list of trip IDs.
        payment = payment_service.create(
            dto.model_dump(mode="json") | {"request_token": request_token},
            actor=g.current_user,
        )
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    except sqlite3.IntegrityError:
        return jsonify(
            {"error": "A selected trip has already been linked to another payment."}
        ), 409
    return jsonify(payment), 201
