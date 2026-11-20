"""TOTP enrollment, replay protection, recovery codes, and throttling."""

from __future__ import annotations

from datetime import datetime, timezone
import hmac
import json
import secrets
import time

import pyotp
from sqlalchemy import select

from ..database import unit_of_work
from ..domain.roles import Role
from ..models import User, UserMFA
from ..security.crypto import blind_index


MFA_REQUIRED_ROLES = frozenset({Role.ADMIN.value, Role.TREASURER.value})
MAX_FAILURES = 5
LOCK_SECONDS = 15 * 60
RECOVERY_CODE_COUNT = 10


class MFAVerificationError(ValueError):
    """Describe a safe MFA verification failure and optional retry delay."""

    def __init__(
        self, message: str, *, retry_after: int = 0, recorded_failure: bool = False
    ) -> None:
        """Store the public error, retry interval, and whether throttling state changed."""
        super().__init__(message)
        self.retry_after = retry_after
        self.recorded_failure = recorded_failure


def requires_mfa(role: str) -> bool:
    """Return whether policy mandates MFA for the supplied application role."""
    return role in MFA_REQUIRED_ROLES


def mfa_status(user_id: int) -> str:
    """Return ``absent``, ``pending``, or ``enabled`` for one account."""
    with unit_of_work() as database_session:
        enrollment = database_session.get(UserMFA, user_id)
        if enrollment is None:
            return "absent"
        return "enabled" if enrollment.enabled_at else "pending"


def start_enrollment(user_id: int) -> str:
    """Replace any incomplete enrollment and return its new random TOTP secret."""
    secret = pyotp.random_base32()
    with unit_of_work() as database_session:
        enrollment = database_session.get(UserMFA, user_id)
        if enrollment and enrollment.enabled_at:
            raise MFAVerificationError("MFA is already enabled for this account.")
        now = int(time.time())
        if enrollment and enrollment.locked_until > now:
            raise MFAVerificationError(
                "Too many incorrect codes. Try again later.",
                retry_after=enrollment.locked_until - now,
            )
        if enrollment is None:
            enrollment = UserMFA(user_id=user_id, secret=secret, recovery_code_hashes="[]")
            database_session.add(enrollment)
        else:
            enrollment.secret = secret
            enrollment.recovery_code_hashes = "[]"
            enrollment.last_totp_step = None
        return secret


def provisioning_uri(secret: str, username: str) -> str:
    """Build the standards-compatible authenticator-app enrollment URI."""
    return pyotp.TOTP(secret).provisioning_uri(
        name=username,
        issuer_name="Club Gift Aid Manager",
    )


def _matching_step(secret: str, code: str, now: int) -> int | None:
    """Return the accepted 30-second counter within the one-step clock window."""
    if len(code) != 6 or not code.isascii() or not code.isdigit():
        return None
    current_step = now // 30
    totp = pyotp.TOTP(secret)
    for step in range(current_step - 1, current_step + 2):
        if hmac.compare_digest(totp.generate_otp(step), code):
            return step
    return None


def _register_failure(enrollment: UserMFA, now: int) -> None:
    """Increment verification failures and impose a fixed lock at the threshold."""
    enrollment.failed_attempts += 1
    if enrollment.failed_attempts >= MAX_FAILURES:
        enrollment.failed_attempts = 0
        enrollment.locked_until = now + LOCK_SECONDS


def verify_totp(
    user_id: int, code: str, *, enable: bool = False, now: int | None = None
) -> list[str]:
    """Verify a fresh TOTP code and optionally enable MFA with recovery codes.

    A code from the immediately previous or next time step is accepted for modest
    device-clock drift. A time step can succeed only once, preventing replay.
    When ``enable`` is true, ten random recovery codes are returned exactly once.
    """
    timestamp = int(time.time()) if now is None else int(now)
    failure: MFAVerificationError | None = None
    recovery_codes: list[str] = []
    with unit_of_work() as database_session:
        enrollment = database_session.get(UserMFA, user_id)
        if enrollment is None or (enable is False and not enrollment.enabled_at):
            raise MFAVerificationError("MFA is not ready for this account.")
        if enrollment.locked_until > timestamp:
            raise MFAVerificationError(
                "Too many incorrect codes. Try again later.",
                retry_after=enrollment.locked_until - timestamp,
            )
        step = _matching_step(enrollment.secret, code.strip(), timestamp)
        if step is None or (
            enrollment.last_totp_step is not None and step <= enrollment.last_totp_step
        ):
            _register_failure(enrollment, timestamp)
            failure = MFAVerificationError(
                "The authentication code is incorrect or has expired.", recorded_failure=True
            )
        else:
            enrollment.last_totp_step = step
            enrollment.failed_attempts = 0
            enrollment.locked_until = 0
            if enable:
                recovery_codes = _new_recovery_codes()
                enrollment.recovery_code_hashes = json.dumps(
                    [_recovery_code_hash(code) for code in recovery_codes]
                )
                user = database_session.get(User, user_id)
                if user is None:
                    raise MFAVerificationError("The account no longer exists.")
                user.session_version += 1
    if failure is not None:
        raise failure
    return recovery_codes


def confirm_enrollment(user_id: int) -> None:
    """Activate a verified enrollment after recovery codes are acknowledged."""
    with unit_of_work() as database_session:
        enrollment = database_session.get(UserMFA, user_id)
        if enrollment is None or enrollment.enabled_at or enrollment.recovery_code_hashes == "[]":
            raise MFAVerificationError("MFA enrollment is not ready to complete.")
        enrollment.enabled_at = datetime.now(timezone.utc).isoformat()


def _new_recovery_codes() -> list[str]:
    """Generate high-entropy, human-readable, single-use recovery codes."""
    codes = []
    for _ in range(RECOVERY_CODE_COUNT):
        raw = secrets.token_hex(8).upper()
        codes.append("-".join(raw[index : index + 4] for index in range(0, 16, 4)))
    return codes


def _normalise_recovery_code(code: str) -> str:
    """Remove visual separators and normalize recovery-code case."""
    return "".join(character for character in code.upper() if character.isalnum())


def _recovery_code_hash(code: str) -> str:
    """Create a keyed, non-reversible index for a high-entropy recovery code."""
    return blind_index(f"mfa-recovery:{_normalise_recovery_code(code)}")


def consume_recovery_code(user_id: int, code: str, *, now: int | None = None) -> None:
    """Atomically consume one valid recovery code or register a failed attempt."""
    timestamp = int(time.time()) if now is None else int(now)
    candidate = _recovery_code_hash(code)
    failure: MFAVerificationError | None = None
    with unit_of_work() as database_session:
        enrollment = database_session.get(UserMFA, user_id)
        if enrollment is None or not enrollment.enabled_at:
            raise MFAVerificationError("MFA is not enabled for this account.")
        if enrollment.locked_until > timestamp:
            raise MFAVerificationError(
                "Too many incorrect codes. Try again later.",
                retry_after=enrollment.locked_until - timestamp,
            )
        hashes = json.loads(enrollment.recovery_code_hashes)
        match = next((stored for stored in hashes if hmac.compare_digest(stored, candidate)), None)
        if match is None:
            _register_failure(enrollment, timestamp)
            failure = MFAVerificationError(
                "The recovery code is incorrect or has already been used.", recorded_failure=True
            )
        else:
            hashes.remove(match)
            enrollment.recovery_code_hashes = json.dumps(hashes)
            enrollment.failed_attempts = 0
            enrollment.locked_until = 0
    if failure is not None:
        raise failure


def reset_mfa(user_id: int) -> bool:
    """Delete an enrollment and invalidate every session for the target user."""
    with unit_of_work() as database_session:
        enrollment = database_session.get(UserMFA, user_id)
        user = database_session.scalar(select(User).where(User.id == user_id))
        if user is None:
            return False
        if enrollment is not None:
            database_session.delete(enrollment)
        user.session_version += 1
        return enrollment is not None
