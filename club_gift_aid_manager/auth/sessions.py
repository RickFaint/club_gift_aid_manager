"""Issue, validate, expire, and revoke individual authenticated sessions."""

from __future__ import annotations

import hashlib
import secrets
import time

from flask import current_app, session
from sqlalchemy import delete, select

from ..database import unit_of_work
from ..models import AuthSession


def _digest(session_id: str) -> str:
    """Derive the irreversible identifier stored for a browser session.

    Only the browser receives the random bearer value. Persisting its SHA-256 digest
    prevents a database read from directly revealing reusable session credentials.
    """
    return hashlib.sha256(session_id.encode("ascii")).hexdigest()


def register_authenticated_session(user_id: int, *, now: int | None = None) -> str:
    """Persist a session record and return the unguessable browser-side ID."""
    issued_at = int(time.time()) if now is None else int(now)
    session_id = secrets.token_urlsafe(32)
    with unit_of_work() as database_session:
        database_session.add(
            AuthSession(
                id=_digest(session_id),
                user_id=user_id,
                created_at=issued_at,
                last_seen_at=issued_at,
            )
        )
    return session_id


def establish_authenticated_session(user: dict, *, authenticated_at: int | None = None) -> None:
    """Replace browser state and register a new, independently revocable session."""
    now = int(time.time()) if authenticated_at is None else int(authenticated_at)
    session_id = register_authenticated_session(user["id"], now=now)
    session.clear()
    session["user_id"] = user["id"]
    session["session_version"] = user["session_version"]
    session["authenticated_at"] = now
    session["auth_session_id"] = session_id
    session["_csrf_token"] = secrets.token_urlsafe(32)
    session.permanent = True


def authenticated_session_is_valid(
    user_id: int,
    *,
    now: int | None = None,
    touch: bool = True,
) -> bool:
    """Validate a browser session and optionally refresh its idle timestamp.

    Ordinary application requests count as activity and therefore update the
    persisted idle timestamp. High-frequency background-job polling can opt out
    because writing the same authentication row on every poll would compete
    with the SQLite transaction doing the actual background work.
    """
    session_id = session.get("auth_session_id")
    if not isinstance(session_id, str) or not session_id:
        return False
    current_time = int(time.time()) if now is None else int(now)
    idle_seconds = int(current_app.config["SESSION_IDLE_SECONDS"])
    absolute_seconds = int(current_app.config["SESSION_ABSOLUTE_SECONDS"])
    with unit_of_work() as database_session:
        record = database_session.scalar(
            select(AuthSession).where(AuthSession.id == _digest(session_id))
        )
        if record is None or record.user_id != user_id:
            return False
        if (
            current_time - record.last_seen_at > idle_seconds
            or current_time - record.created_at > absolute_seconds
            or current_time < record.created_at
        ):
            database_session.delete(record)
            return False
        if touch:
            record.last_seen_at = current_time
        return True


def revoke_authenticated_session() -> None:
    """Revoke the current browser session even if its cookie was copied."""
    session_id = session.get("auth_session_id")
    if not isinstance(session_id, str) or not session_id:
        return
    with unit_of_work() as database_session:
        database_session.execute(delete(AuthSession).where(AuthSession.id == _digest(session_id)))
