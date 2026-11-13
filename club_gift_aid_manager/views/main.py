"""Main routes.

A Blueprint is Flask's way of collecting related URL handlers.  Defining a
route here does not start a second application: create_app() later registers
this collection on the one Flask application object.
"""

from flask import Blueprint

from .shared import *  # noqa: F403 - shared view vocabulary is intentionally centralised
from ..auth.decorators import roles_required


blueprint = Blueprint("main", __name__)

@blueprint.get("/privacy-notice")
def privacy_notice_current():
    """Serve the current published privacy notice and its proof metadata."""
    return jsonify(get_current_privacy_notice())


@blueprint.get("/privacy-notice/<int:version_id>")
def privacy_notice_historical(version_id: int):
    """Serve an immutable historical notice version by ID."""
    notice = get_privacy_notice_version(version_id)
    if notice is None:
        return jsonify({"error": "Privacy notice version not found."}), 404
    return jsonify(notice)


@blueprint.get("/")
def index():
    """Render the role-aware dashboard shell."""
    return render_template("index.html")


@blueprint.get("/how-it-works")
@roles_required(Role.ADMIN, Role.TREASURER, Role.VOLUNTEER)
def how_it_works_page():
    """Render the role-specific application guides."""
    return render_template("how_it_works.html")


