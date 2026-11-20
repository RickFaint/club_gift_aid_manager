"""Administrator-assisted reset and mandatory temporary-password replacement."""

from __future__ import annotations

import secrets
import time

from flask import (
    Blueprint,
    current_app,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from itsdangerous import BadData, URLSafeTimedSerializer

from .decorators import recent_authentication_required, roles_required
from ..core.validation import parse_dto
from ..db import (
    get_user_by_id,
    record_audit_event,
    replace_temporary_password,
    reset_user_password,
)
from ..domain.roles import Role
from ..dtos import UserPasswordResetDTO
from ..mfa.routes import begin_mfa_pending
from ..mfa.service import requires_mfa


blueprint = Blueprint("passwords", __name__)
PENDING_CHANGE_SECONDS = 10 * 60
INVITATION_MAX_AGE_SECONDS = 48 * 60 * 60


def _invitation_serializer() -> URLSafeTimedSerializer:
    """Separate signed invitation codes from other application tokens."""
    return URLSafeTimedSerializer(current_app.secret_key, salt="gift-aid-account-invitation-v1")


@blueprint.route("/account/activate", methods=["GET", "POST"])
def activate_account():
    """Let an invited account set its password using a one-use, expiring code."""
    if request.method == "GET":
        return render_template("auth/change_temporary_password.html", activation=True)
    code = request.form.get("invitation", "").strip()
    try:
        if not code or len(code) > 512:
            raise BadData("Invalid code")
        user_id, version = _invitation_serializer().loads(code, max_age=INVITATION_MAX_AGE_SECONDS)
        user = get_user_by_id(user_id)
        if (
            user is None
            or not user["is_active"]
            or not user["must_change_password"]
            or user["session_version"] != version
        ):
            raise BadData("Invalid invitation")
    except BadData:
        return render_template(
            "auth/change_temporary_password.html",
            activation=True,
            error="This activation code is invalid or expired. Ask an administrator for a new code.",
        ), 400
    new_password = request.form.get("new_password", "")
    try:
        if new_password != request.form.get("confirm_password", ""):
            raise ValueError("The new password and confirmation do not match.")
        updated = replace_temporary_password(user["id"], new_password)
    except ValueError as exc:
        return render_template(
            "auth/change_temporary_password.html", activation=True, error=str(exc)
        ), 400
    if updated is None:
        return redirect(url_for("passwords.activate_account"))
    record_audit_event(
        actor_user_id=updated["id"],
        actor_username=updated["username"],
        action="account_activated_by_invitation",
        entity_type="authentication",
        entity_id=updated["id"],
    )
    session.clear()
    return redirect(url_for("auth.login_page"))


@blueprint.post("/admin/users/<int:user_id>/invitation")
@roles_required(Role.ADMIN)
@recent_authentication_required()
def admin_issue_invitation(user_id: int):
    """Issue a time-limited code and invalidate earlier credentials and codes."""
    user = get_user_by_id(user_id)
    if user is None:
        return jsonify({"error": "User not found."}), 404
    if not user["is_active"] or not user["must_change_password"]:
        return jsonify({"error": "The account must be active and awaiting password setup."}), 409
    target = reset_user_password(user_id, f"A1!{secrets.token_urlsafe(48)}")
    if target is None:
        return jsonify({"error": "User not found."}), 404
    record_audit_event(
        actor_user_id=g.current_user["id"],
        actor_username=g.current_user["username"],
        action="account_invitation_issued",
        entity_type="user",
        entity_id=user_id,
    )
    return jsonify(
        {
            "code": _invitation_serializer().dumps([user_id, target["session_version"]]),
            "activation_path": url_for("passwords.activate_account"),
            "expires_in_hours": INVITATION_MAX_AGE_SECONDS // 3600,
        }
    )


def begin_password_change(user: dict, *, authenticated_at: int | None = None) -> None:
    """Replace browser state with a restricted password-change-only session."""
    now = int(time.time()) if authenticated_at is None else int(authenticated_at)
    session.clear()
    session["password_change_user_id"] = user["id"]
    session["password_change_session_version"] = user["session_version"]
    session["password_change_authenticated_at"] = now
    session["_csrf_token"] = secrets.token_urlsafe(32)
    session.permanent = False


def _pending_user() -> dict | None:
    """Resolve an active, version-matched temporary-password session."""
    user_id = session.get("password_change_user_id")
    started = session.get("password_change_authenticated_at")
    if not isinstance(user_id, int) or not isinstance(started, int):
        return None
    user = get_user_by_id(user_id)
    if (
        user is None
        or not user["is_active"]
        or not user["must_change_password"]
        or session.get("password_change_session_version") != user["session_version"]
        or int(time.time()) - started > PENDING_CHANGE_SECONDS
    ):
        session.clear()
        return None
    return user


@blueprint.route("/account/change-temporary-password", methods=["GET", "POST"])
def change_temporary_password():
    """Force replacement before granting application or MFA access."""
    user = _pending_user()
    if user is None:
        return redirect(url_for("auth.login_page"))
    if request.method == "GET":
        return render_template("auth/change_temporary_password.html")
    new_password = request.form.get("new_password", "")
    if new_password != request.form.get("confirm_password", ""):
        return render_template(
            "auth/change_temporary_password.html",
            error="The new password and confirmation do not match.",
        ), 400
    try:
        updated = replace_temporary_password(user["id"], new_password)
    except ValueError as exc:
        return render_template("auth/change_temporary_password.html", error=str(exc)), 400
    if updated is None:
        session.clear()
        return redirect(url_for("auth.login_page"))
    record_audit_event(
        actor_user_id=updated["id"],
        actor_username=updated["username"],
        action="password_changed_after_reset",
        entity_type="authentication",
        entity_id=updated["id"],
    )
    if requires_mfa(updated["role"]):
        destination = begin_mfa_pending(updated)
        return redirect(url_for(f"mfa.{destination}"))
    now = int(time.time())
    from .sessions import establish_authenticated_session

    establish_authenticated_session(updated, authenticated_at=now)
    return redirect(url_for("main.index"))


@blueprint.post("/admin/users/<int:user_id>/password-reset")
@roles_required(Role.ADMIN)
@recent_authentication_required()
def admin_reset_password(user_id: int):
    """Assign a temporary password after offline identity verification."""
    dto = parse_dto(UserPasswordResetDTO, request.get_json(silent=True) or {})
    try:
        target = reset_user_password(user_id, dto.temporary_password)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if target is None:
        return jsonify({"error": "User not found."}), 404
    record_audit_event(
        actor_user_id=g.current_user["id"],
        actor_username=g.current_user["username"],
        action="password_reset_by_admin",
        entity_type="user",
        entity_id=user_id,
        changed_fields=("password", "must_change_password"),
    )
    if user_id == g.current_user["id"]:
        session.clear()
    return jsonify({"reset": True, "sessions_invalidated": True, "change_required": True})
