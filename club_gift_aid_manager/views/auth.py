"""Auth routes.

A Blueprint is Flask's way of collecting related URL handlers.  Defining a
route here does not start a second application: create_app() later registers
this collection on the one Flask application object.
"""

from flask import Blueprint

from .shared import *  # noqa: F403 - shared view vocabulary is intentionally centralised
from ..auth.decorators import roles_required


blueprint = Blueprint("auth", __name__)

@blueprint.get("/login")
def login_page():
    """Render the sign-in form and its session-scoped CSRF token."""
    return render_template("login.html")


@blueprint.post("/login")
def login():
    """Verify a password hash, rotate the session, and send the user to home."""
    username = request.form.get("username", "")
    password = request.form.get("password", "")
    attempt_keys = login_attempt_keys(username, request.remote_addr)
    now = int(time.time())
    lock_seconds = get_login_lock_seconds(attempt_keys, now=now)
    if lock_seconds:
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
        session.clear()
        session["user_id"] = user["id"]
        # The version binds this browser session to the account's current
        # permissions.  Administrative security changes increment it.
        session["session_version"] = user["session_version"]
        session["authenticated_at"] = now
        session["_csrf_token"] = secrets.token_urlsafe(32)
        session.permanent = True
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
        hmac.new(secret, value.encode("utf-8"), hashlib.sha256).hexdigest()
        for value in values
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
    session.clear()
    return redirect(url_for("auth.login_page"))


