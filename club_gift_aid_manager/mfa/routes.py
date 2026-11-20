"""HTTP flows for mandatory TOTP enrollment, challenges, and recovery."""

from __future__ import annotations

import base64
from io import BytesIO
import secrets
import time

from flask import Blueprint, g, jsonify, redirect, render_template, request, session, url_for
import qrcode  # type: ignore[import-untyped]
from qrcode.image.svg import SvgPathImage  # type: ignore[import-untyped]

from ..auth.decorators import recent_authentication_required, roles_required
from ..db import get_user_by_id, record_audit_event
from ..domain.roles import Role
from .service import (
    MFAVerificationError,
    confirm_enrollment,
    consume_recovery_code,
    mfa_status,
    provisioning_uri,
    requires_mfa,
    reset_mfa,
    start_enrollment,
    verify_totp,
)


blueprint = Blueprint("mfa", __name__)
PENDING_SESSION_SECONDS = 10 * 60


def begin_mfa_pending(user: dict, *, authenticated_at: int | None = None) -> str:
    """Replace browser state with a short-lived, application-inaccessible MFA session."""
    now = int(time.time()) if authenticated_at is None else int(authenticated_at)
    session.clear()
    session["mfa_pending_user_id"] = user["id"]
    session["mfa_pending_session_version"] = user["session_version"]
    session["mfa_pending_authenticated_at"] = now
    session["_csrf_token"] = secrets.token_urlsafe(32)
    session.permanent = False
    return "setup" if mfa_status(user["id"]) != "enabled" else "challenge"


def _pending_user() -> dict | None:
    """Resolve a valid pending user without promoting it to an authenticated session."""
    user_id = session.get("mfa_pending_user_id")
    started = session.get("mfa_pending_authenticated_at")
    if not isinstance(user_id, int) or not isinstance(started, int):
        return None
    user = get_user_by_id(user_id)
    if (
        user is None
        or not user["is_active"]
        or not requires_mfa(user["role"])
        or session.get("mfa_pending_session_version") != user["session_version"]
        or int(time.time()) - started > PENDING_SESSION_SECONDS
    ):
        session.clear()
        return None
    return user


def _complete_login(user: dict, method: str) -> None:
    """Rotate pending state into a complete authenticated session and audit it."""
    now = int(time.time())
    from ..auth.sessions import establish_authenticated_session

    establish_authenticated_session(user, authenticated_at=now)
    session["mfa_verified_at"] = now
    record_audit_event(
        actor_user_id=user["id"],
        actor_username=user["username"],
        action="login_succeeded",
        entity_type="authentication",
        entity_id=user["id"],
        changed_fields=(method,),
    )


def _qr_data_uri(uri: str) -> str:
    """Render a provisioning URI as an inline SVG QR image without external requests."""
    image = qrcode.make(uri, image_factory=SvgPathImage)
    output = BytesIO()
    image.save(output)
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


@blueprint.get("/mfa/setup")
def setup():
    """Explain mandatory enrollment before generating any authenticator secret."""
    user = _pending_user()
    if user is None:
        return redirect(url_for("auth.login_page"))
    if mfa_status(user["id"]) == "enabled":
        return redirect(url_for("mfa.challenge"))
    return render_template("mfa/setup.html", pending_user=user)


@blueprint.post("/mfa/setup/start")
def setup_start():
    """Create a pending secret and show its QR code and manual entry value."""
    user = _pending_user()
    if user is None:
        return redirect(url_for("auth.login_page"))
    try:
        secret = start_enrollment(user["id"])
    except MFAVerificationError as exc:
        return render_template(
            "mfa/setup.html", pending_user=user, error=str(exc)
        ), 429 if exc.retry_after else 400
    uri = provisioning_uri(secret, user["username"])
    return render_template(
        "mfa/enroll.html",
        pending_user=user,
        qr_data_uri=_qr_data_uri(uri),
        manual_secret=secret,
    )


@blueprint.post("/mfa/setup/verify")
def setup_verify():
    """Confirm the first TOTP code, enable MFA, and display recovery codes once."""
    user = _pending_user()
    if user is None:
        return redirect(url_for("auth.login_page"))
    try:
        recovery_codes = verify_totp(user["id"], request.form.get("code", ""), enable=True)
    except MFAVerificationError as exc:
        g.mfa_failure_recorded = exc.recorded_failure
        return render_template(
            "mfa/setup.html",
            pending_user=user,
            error=str(exc),
            restart_required=True,
        ), 429 if exc.retry_after else 400
    refreshed = get_user_by_id(user["id"])
    if refreshed is None:
        session.clear()
        return redirect(url_for("auth.login_page"))
    session["mfa_pending_session_version"] = refreshed["session_version"]
    session["mfa_recovery_ack_required"] = True
    return render_template("mfa/recovery_codes.html", recovery_codes=recovery_codes)


@blueprint.post("/mfa/setup/complete")
def setup_complete():
    """Require recovery-code acknowledgement before issuing the full session."""
    user = _pending_user()
    if user is None:
        return redirect(url_for("auth.login_page"))
    if not session.get("mfa_recovery_ack_required") or request.form.get("saved") != "yes":
        return jsonify({"error": "Confirm that the recovery codes have been saved."}), 400
    try:
        confirm_enrollment(user["id"])
    except MFAVerificationError as exc:
        session.clear()
        return jsonify({"error": str(exc)}), 400
    record_audit_event(
        actor_user_id=user["id"],
        actor_username=user["username"],
        action="mfa_enrolled",
        entity_type="authentication",
        entity_id=user["id"],
    )
    _complete_login(user, "totp_enrollment")
    return redirect(url_for("main.index"))


@blueprint.route("/mfa/challenge", methods=["GET", "POST"])
def challenge():
    """Complete a pending login with a TOTP or single-use recovery code."""
    user = _pending_user()
    if user is None:
        return redirect(url_for("auth.login_page"))
    if mfa_status(user["id"]) != "enabled":
        return redirect(url_for("mfa.setup"))
    if request.method == "GET":
        return render_template("mfa/challenge.html", pending_user=user)
    method = request.form.get("method", "totp")
    try:
        if method == "recovery":
            consume_recovery_code(user["id"], request.form.get("code", ""))
        else:
            method = "totp"
            verify_totp(user["id"], request.form.get("code", ""))
    except MFAVerificationError as exc:
        g.mfa_failure_recorded = exc.recorded_failure
        return render_template(
            "mfa/challenge.html",
            pending_user=user,
            selected_method=method,
            error=str(exc),
        ), 429 if exc.retry_after else 401
    _complete_login(user, method)
    return redirect(url_for("main.index"))


@blueprint.post("/mfa/cancel")
def cancel():
    """Discard pending authentication state and return to the login page."""
    session.clear()
    return redirect(url_for("auth.login_page"))


@blueprint.post("/admin/users/<int:user_id>/mfa-reset")
@roles_required(Role.ADMIN)
@recent_authentication_required()
def admin_reset(user_id: int):
    """Reset another user's MFA after offline identity verification and audit it."""
    target = get_user_by_id(user_id)
    if target is None:
        return jsonify({"error": "User not found."}), 404
    was_enrolled = reset_mfa(user_id)
    record_audit_event(
        actor_user_id=g.current_user["id"],
        actor_username=g.current_user["username"],
        action="mfa_reset",
        entity_type="user",
        entity_id=user_id,
        changed_fields=("mfa",),
    )
    if user_id == g.current_user["id"]:
        session.clear()
    return jsonify({"reset": was_enrolled, "sessions_invalidated": True})
