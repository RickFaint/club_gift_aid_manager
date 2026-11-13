"""People routes.

A Blueprint is Flask's way of collecting related URL handlers.  Defining a
route here does not start a second application: create_app() later registers
this collection on the one Flask application object.
"""

from flask import Blueprint

from .shared import *  # noqa: F403 - shared view vocabulary is intentionally centralised
from ..auth.decorators import roles_required
from ..validation import as_bool, require_fields


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
    if g.current_user["role"] == Role.VOLUNTEER.value:
        person = get_person(g.current_user["person_id"])
        all_items = [person] if person else []
        return jsonify(paginated_response(
            all_items[offset : offset + limit], len(all_items), limit, offset
        ))
    items, total = list_people(
        limit=limit, offset=offset, with_total=True
    )
    return jsonify(paginated_response(items, total, limit, offset))


@blueprint.post("/people")
@roles_required(Role.ADMIN, Role.TREASURER)
def people_create():
    """Validate required contact fields and create a person record."""
    payload = get_payload()
    require_fields(payload, ("name",))

    person = create_person(
        name=payload["name"],
        email=payload.get("email"),
        phone=payload.get("phone"),
        role=payload.get("role", Role.VOLUNTEER.value),
        active=as_bool(payload.get("active"), default=True),
        notes=payload.get("notes"),
    )
    audit_change("created", "person", person["id"], person.keys())
    return jsonify(person), 201


@blueprint.get("/people/<int:person_id>")
@roles_required(Role.ADMIN, Role.TREASURER, Role.VOLUNTEER)
def people_get(person_id: int):
    """Fetch one person, applying the volunteer's own-record restriction."""
    if g.current_user["role"] == Role.VOLUNTEER.value and person_id != g.current_user["person_id"]:
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
    return jsonify({
        "person": person,
        "trips": list_trips(person_id),
        "declarations": list_declarations(person_id),
        "donations": list_donations(person_id),
        "payments": list_payments(person_id),
    })


@blueprint.put("/people/<int:person_id>")
@roles_required(Role.ADMIN, Role.TREASURER)
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


@blueprint.delete("/people/<int:person_id>")
@roles_required(Role.ADMIN, Role.TREASURER)
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


