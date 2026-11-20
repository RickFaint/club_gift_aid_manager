"""Auth routes.

A Blueprint is Flask's way of collecting related URL handlers.  Defining a
route here does not start a second application: create_app() later registers
this collection on the one Flask application object.
"""

import logging

from flask import Blueprint

from .shared import (
    DUMMY_PASSWORD_HASH,
    check_password_hash,
    clear_login_failures,
    current_app,
    g,
    get_login_lock_seconds,
    get_user_by_username,
    hashlib,
    hmac,
    record_audit_event,
    record_login_failure,
    redirect,
    render_template,
    request,
    secrets,
    session,
    time,
    url_for,
)
from ..auth.decorators import roles_required
from ..security.proxy import ProxyAddressError, resolve_client_address
from ..core.logging import log_event


blueprint = Blueprint("auth", __name__)
logger = logging.getLogger(__name__)


@blueprint.get("/login")
def login_page():
    """Render the sign-in form and its session-scoped CSRF token."""
    return render_template("login.html")


@blueprint.post("/login")
def login():
    """Verify a password hash, rotate the session, and send the user to home."""
    username = request.form.get("username", "")
    password = request.form.get("password", "")
    try:
        client_address = resolve_client_address(request, current_app.config["PROXY_MODE"])
    except ProxyAddressError:
        # Do not process a password when the configured trusted proxy contract is broken.
        return render_template(
            "login.html", error="Unable to verify the request source. Try again later."
        ), 400
    attempt_keys = login_attempt_keys(username, client_address)
    now = int(time.time())
    lock_seconds = get_login_lock_seconds(attempt_keys, now=now)
    if lock_seconds:
        log_event(
            logger,
            "login_throttled",
            level=logging.WARNING,
            request_id=getattr(g, "request_id", None),
            status_code=429,
            outcome="blocked",
        )
        return render_template(
            "login.html",
            error="Incorrect username or password. Try again later.",
        ), 429

    user = get_user_by_username(username)
    password_matches = check_password_hash(
        user["password_hash"] if user else DUMMY_PASSWORD_HASH,
        password,
    )
    if user and user["is_active"] and password_matches:
        clear_login_failures(attempt_keys)
        if user["must_change_password"]:
            from ..auth.passwords import begin_password_change

            begin_password_change(user, authenticated_at=now)
            record_audit_event(
                actor_user_id=user["id"],
                actor_username=user["username"],
                action="temporary_password_verified",
                entity_type="authentication",
                entity_id=user["id"],
            )
            return redirect(url_for("passwords.change_temporary_password"))
        from ..mfa.routes import begin_mfa_pending
        from ..mfa.service import requires_mfa

        if requires_mfa(user["role"]):
            destination = begin_mfa_pending(user, authenticated_at=now)
            record_audit_event(
                actor_user_id=user["id"],
                actor_username=user["username"],
                action="password_verified_mfa_pending",
                entity_type="authentication",
                entity_id=user["id"],
            )
            return redirect(url_for(f"mfa.{destination}"))
        from ..auth.sessions import establish_authenticated_session

        establish_authenticated_session(user, authenticated_at=now)
        record_audit_event(
            actor_user_id=user["id"],
            actor_username=user["username"],
            action="login_succeeded",
            entity_type="authentication",
            entity_id=user["id"],
        )
        return redirect(url_for("main.index"))

    newly_locked, lock_seconds = record_login_failure(
        attempt_keys,
        now=now,
        window_seconds=15 * 60,
        max_failures={attempt_keys[0]: 30, attempt_keys[1]: 5},
        lockout_seconds=15 * 60,
    )
    record_audit_event(
        actor_user_id=None,
        actor_username="anonymous",
        action="login_failed",
        entity_type="authentication",
        entity_id=attempt_keys[1],
    )
    if newly_locked:
        record_audit_event(
            actor_user_id=None,
            actor_username="anonymous",
            action="login_rate_limited",
            entity_type="authentication",
            entity_id=attempt_keys[0],
        )
        log_event(
            logger,
            "login_throttled",
            level=logging.WARNING,
            request_id=getattr(g, "request_id", None),
            status_code=429,
            outcome="new_lock",
        )
    return render_template(
        "login.html",
        error=(
            "Incorrect username or password. Try again later."
            if lock_seconds
            else "Incorrect username or password."
        ),
    ), 429 if lock_seconds else 401


def login_attempt_keys(username, remote_address):
    """Derive non-reversible rate-limit keys without storing usernames or IP addresses."""
    normalized_user = username.strip().casefold()[:256]
    normalized_address = remote_address or "unknown"
    # ``current_app`` refers to the factory-created Flask instance handling the
    # request; Blueprint modules do not own a global application object.
    secret = current_app.config["SECRET_KEY"].encode("utf-8")
    values = (
        f"ip:{normalized_address}",
        f"ip-user:{normalized_address}:{normalized_user}",
    )
    return tuple(
        hmac.new(secret, value.encode("utf-8"), hashlib.sha256).hexdigest() for value in values
    )


@blueprint.post("/logout")
def logout():
    """Discard login and CSRF state before returning to the sign-in page."""
    user = g.current_user
    record_audit_event(
        actor_user_id=user["id"],
        actor_username=user["username"],
        action="logout",
        entity_type="authentication",
        entity_id=user["id"],
    )
    from ..auth.sessions import revoke_authenticated_session

    revoke_authenticated_session()
    session.clear()
    return redirect(url_for("auth.login_page"))
