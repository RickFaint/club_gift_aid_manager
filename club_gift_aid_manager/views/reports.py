"""Reports routes.

A Blueprint is Flask's way of collecting related URL handlers.  Defining a
route here does not start a second application: create_app() later registers
this collection on the one Flask application object.
"""

from flask import Blueprint

from .shared import *  # noqa: F403 - shared view vocabulary is intentionally centralised
from ..auth.decorators import roles_required


blueprint = Blueprint("reports", __name__)

@blueprint.get("/reports/page")
@roles_required(Role.ADMIN)
def reports_page():
    """Render the administrator-only financial and volunteer report."""
    return render_template("reports.html")


@blueprint.get("/reports/summary")
@roles_required(Role.ADMIN, Role.TREASURER)
def reports_summary():
    """Return high-level dashboard counters and financial totals."""
    return jsonify(get_summary())


@blueprint.get("/dashboard/overview")
@roles_required(Role.ADMIN, Role.TREASURER, Role.VOLUNTEER)
def dashboard_overview():
    """Return dashboard prompts scoped to the signed-in user's role."""
    if g.current_user["role"] == Role.VOLUNTEER.value:
        return jsonify(get_person_dashboard(g.current_user["person_id"]))

    overview = get_dashboard_checks()
    if g.current_user["role"] == Role.ADMIN.value:
        overview["recent_activity"] = list_audit_events(limit=5)
    return jsonify(overview)


@blueprint.get("/reports/payment-reconciliation")
@roles_required(Role.ADMIN, Role.TREASURER)
def reports_payment_reconciliation():
    """Return per-person payments minus donations explicitly linked to payments."""
    return jsonify({"items": list_payment_reconciliation()})


@blueprint.get("/reports/financial")
@roles_required(Role.ADMIN)
def reports_financial():
    """Return club totals and per-volunteer trip and finance aggregates."""
    return jsonify(get_financial_report())


