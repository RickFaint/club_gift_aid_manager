"""Finance routes.

A Blueprint is Flask's way of collecting related URL handlers.  Defining a
route here does not start a second application: create_app() later registers
this collection on the one Flask application object.
"""

from flask import Blueprint

from .shared import *  # noqa: F403 - shared view vocabulary is intentionally centralised
from ..auth.decorators import roles_required
from ..validation import (
    as_bool, as_int, as_non_negative_int, as_optional_date, as_text, require_fields,
)


blueprint = Blueprint("finance", __name__)

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
        person_id, limit=limit, offset=offset, with_total=True
    )
    return jsonify(paginated_response(items, total, limit, offset))


@blueprint.post("/declarations")
@roles_required(Role.ADMIN, Role.TREASURER)
def declarations_create():
    """Create a donor declaration and record its creation in the audit log."""
    payload = get_payload()
    require_fields(payload, ("person_id", "full_name"))

    notice_version_id = payload.get("privacy_notice_version_id")
    if notice_version_id not in (None, ""):
        try:
            shown_notice = get_privacy_notice_version(as_int(notice_version_id, "privacy_notice_version_id"))
        except (TypeError, ValueError):
            shown_notice = None
        if shown_notice is None or not hmac.compare_digest(
            shown_notice["text_sha256"], str(payload.get("privacy_notice_sha256") or ""),
        ):
            return jsonify({"error": "The referenced privacy notice version is invalid. Refresh the page and try again."}), 400
        notice_version_id = shown_notice["id"]
    else:
        notice_version_id = None

    declaration = create_declaration(
        person_id=as_int(payload["person_id"], "person_id"),
        full_name=as_text(payload["full_name"], "full_name", maximum=200, required=True),
        home_address=as_text(payload.get("home_address"), "home_address", maximum=500),
        postcode=as_text(payload.get("postcode"), "postcode", maximum=20),
        declaration_date=as_optional_date(payload.get("declaration_date"), "declaration_date"),
        scope=as_text(payload.get("scope", "future"), "scope", maximum=50, required=True),
        withdrawn_date=as_optional_date(payload.get("withdrawn_date"), "withdrawn_date"),
        evidence_ref=as_text(payload.get("evidence_ref"), "evidence_ref", maximum=500),
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
    for field, maximum in {"full_name": 200, "home_address": 500, "postcode": 20, "evidence_ref": 500}.items():
        if field in payload:
            payload[field] = as_text(payload[field], field, maximum=maximum, required=field == "full_name")
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
        person_id, limit=limit, offset=offset, with_total=True
    )
    return jsonify(paginated_response(items, total, limit, offset))


@blueprint.post("/donations")
@roles_required(Role.ADMIN, Role.TREASURER)
def donations_create():
    """Create a donation after validating its person and optional associations."""
    payload = get_payload()
    require_fields(payload, ("person_id", "amount_pence"))

    try:
        donation = create_donation(
            person_id=as_int(payload["person_id"], "person_id"),
            amount_pence=as_non_negative_int(payload["amount_pence"], "amount_pence"),
            donated_date=as_optional_date(payload.get("donated_date"), "donated_date"),
            method=as_text(payload.get("method"), "method", maximum=100),
            reference=as_text(payload.get("reference"), "reference", maximum=500),
            declaration_id=as_int(payload["declaration_id"], "declaration_id") if payload.get("declaration_id") else None,
            gift_aid_eligible=as_bool(payload.get("gift_aid_eligible")),
            gift_aid_claimed_date=as_optional_date(payload.get("gift_aid_claimed_date"), "gift_aid_claimed_date"),
            linked_payment_ids=payload.get("linked_payment_ids") if "linked_payment_ids" in payload else None,
            linked_payment_id=as_int(payload["linked_payment_id"], "linked_payment_id") if payload.get("linked_payment_id") else None,
            received_date=as_optional_date(payload.get("received_date"), "received_date"),
            bank_reference=as_text(payload.get("bank_reference"), "bank_reference", maximum=500),
            receipt_id=payload.get("receipt_id"),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    audit_change("created", "donation", donation["id"], donation.keys())
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


@blueprint.get("/payments")
@roles_required(Role.ADMIN, Role.TREASURER)
def payments_index():
    """List reimbursements with their linked trips and donation totals."""
    person_id = request.args.get("person_id", type=int)
    limit, offset = get_pagination()
    items, total = list_payments(
        person_id, limit=limit, offset=offset, with_total=True
    )
    return jsonify(paginated_response(items, total, limit, offset))


@blueprint.post("/payments")
@roles_required(Role.ADMIN, Role.TREASURER)
def payments_create():
    """Create a payment and optionally link multiple fully-covered trips."""
    payload = get_payload()
    require_fields(payload, ("person_id", "amount_pence"))

    try:
        # Multi-select form values are serialized as a JSON list of trip IDs.
        trip_ids = payload.get("trip_ids", [])
        if not isinstance(trip_ids, list):
            return jsonify({"error": "Linked trips must be supplied as a list."}), 400
        payment = create_payment(
            person_id=as_int(payload["person_id"], "person_id"),
            amount_pence=as_non_negative_int(payload["amount_pence"], "amount_pence"),
            paid_date=as_optional_date(payload.get("paid_date"), "paid_date"),
            method=as_text(payload.get("method", "bank transfer"), "method", maximum=100, required=True),
            reference=as_text(payload.get("reference"), "reference", maximum=500),
            batch_id=as_text(payload.get("batch_id"), "batch_id", maximum=200),
            trip_ids=trip_ids,
        )
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    except sqlite3.IntegrityError:
        return jsonify({"error": "A selected trip has already been linked to another payment."}), 409
    audit_change("created", "payment", payment["id"], ("person_id", "amount_pence", "paid_date", "method", "reference", "trip_ids"))
    return jsonify(payment), 201


