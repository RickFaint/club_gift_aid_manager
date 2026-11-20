"""End-to-end security tests for mandatory staff TOTP authentication."""

import re

import pyotp

from app import app
from club_gift_aid_manager.database import unit_of_work
from club_gift_aid_manager.db import create_user, get_user_by_id, list_audit_events
from club_gift_aid_manager.models import UserMFA
from club_gift_aid_manager.mfa.service import MAX_FAILURES, mfa_status


PASSWORD = "Secure-Test-Password1!"


def _csrf(client) -> str:
    """Return the CSRF token from a test client's signed session."""
    with client.session_transaction() as browser_session:
        return browser_session["_csrf_token"]


def _password_login(client, username: str) -> object:
    """Submit a valid password through the CSRF-protected login flow."""
    client.get("/login")
    return client.post(
        "/login",
        data={"username": username, "password": PASSWORD, "_csrf_token": _csrf(client)},
    )


def _stored_secret(user_id: int) -> str:
    """Read a test enrollment secret through the encrypted model mapping."""
    with unit_of_work() as database_session:
        enrollment = database_session.get(UserMFA, user_id)
        assert enrollment is not None
        return enrollment.secret


def _enroll(client, user: dict) -> list[str]:
    """Complete the browser enrollment flow and return its one-time recovery codes."""
    assert _password_login(client, user["username"]).location.endswith("/mfa/setup")
    started = client.post(
        "/mfa/setup/start",
        data={"_csrf_token": _csrf(client)},
    )
    assert started.status_code == 200
    assert b"Authenticator enrollment QR code" in started.data
    code = pyotp.TOTP(_stored_secret(user["id"])).now()
    verified = client.post(
        "/mfa/setup/verify",
        data={"code": code, "_csrf_token": _csrf(client)},
    )
    assert verified.status_code == 200
    recovery_codes = re.findall(rb"[A-F0-9]{4}(?:-[A-F0-9]{4}){3}", verified.data)
    assert len(recovery_codes) == 10
    return [code.decode("ascii") for code in recovery_codes]


def test_admin_password_enters_restricted_enrollment_session():
    """A staff password alone cannot access application data before enrollment."""
    user = create_user(username="mfa-admin", password=PASSWORD, role="admin")
    client = app.test_client()

    response = _password_login(client, user["username"])

    assert response.status_code == 302
    assert response.location.endswith("/mfa/setup")
    assert client.get("/people").status_code == 401
    with client.session_transaction() as browser_session:
        assert browser_session["mfa_pending_user_id"] == user["id"]
        assert "user_id" not in browser_session


def test_enrollment_requires_acknowledgement_then_issues_full_session():
    """Recovery codes are shown once and must be acknowledged before application access."""
    user = create_user(username="mfa-treasurer", password=PASSWORD, role="treasurer")
    client = app.test_client()
    recovery_codes = _enroll(client, user)

    missing_ack = client.post(
        "/mfa/setup/complete",
        data={"_csrf_token": _csrf(client)},
    )
    assert missing_ack.status_code == 400
    completed = client.post(
        "/mfa/setup/complete",
        data={"saved": "yes", "_csrf_token": _csrf(client)},
    )

    assert completed.status_code == 302
    assert completed.location.endswith("/")
    assert mfa_status(user["id"]) == "enabled"
    assert len(recovery_codes) == 10
    with client.session_transaction() as browser_session:
        assert browser_session["user_id"] == user["id"]
        assert isinstance(browser_session["mfa_verified_at"], int)
        assert "mfa_pending_user_id" not in browser_session


def test_recovery_code_is_single_use_and_completes_a_later_login():
    """A saved recovery code authenticates exactly once and is never stored plaintext."""
    user = create_user(username="mfa-recovery", password=PASSWORD, role="admin")
    client = app.test_client()
    recovery_code = _enroll(client, user)[0]
    client.post(
        "/mfa/setup/complete",
        data={"saved": "yes", "_csrf_token": _csrf(client)},
    )
    client.post("/logout", data={"_csrf_token": _csrf(client)})

    assert _password_login(client, user["username"]).location.endswith("/mfa/challenge")
    success = client.post(
        "/mfa/challenge",
        data={"method": "recovery", "code": recovery_code, "_csrf_token": _csrf(client)},
    )
    assert success.status_code == 302
    client.post("/logout", data={"_csrf_token": _csrf(client)})
    _password_login(client, user["username"])
    reused = client.post(
        "/mfa/challenge",
        data={"method": "recovery", "code": recovery_code, "_csrf_token": _csrf(client)},
    )
    assert reused.status_code == 401


def test_mfa_challenge_persists_a_shared_lockout_for_totp_and_recovery_codes():
    """Rejected HTTP challenges count against the account across both verification methods."""
    user = create_user(username="mfa-throttled", password=PASSWORD, role="admin")
    client = app.test_client()
    recovery_code = _enroll(client, user)[0]
    client.post("/mfa/setup/complete", data={"saved": "yes", "_csrf_token": _csrf(client)})
    client.post("/logout", data={"_csrf_token": _csrf(client)})
    assert _password_login(client, user["username"]).location.endswith("/mfa/challenge")

    for i in range(MAX_FAILURES):
        response = client.post(
            "/mfa/challenge",
            data={
                "method": "totp" if i % 2 else "recovery",
                "code": "wrong",
                "_csrf_token": _csrf(client),
            },
        )
        assert response.status_code == 401

    locked = client.post(
        "/mfa/challenge",
        data={"method": "recovery", "code": recovery_code, "_csrf_token": _csrf(client)},
    )
    assert locked.status_code == 429
    assert client.get("/people").status_code == 401


def test_restarting_setup_does_not_clear_mfa_throttling():
    """A new enrollment QR cannot reset failed verification counts."""
    user = create_user(username="mfa-setup-throttled", password=PASSWORD, role="admin")
    client = app.test_client()
    assert _password_login(client, user["username"]).location.endswith("/mfa/setup")

    for _ in range(MAX_FAILURES):
        started = client.post("/mfa/setup/start", data={"_csrf_token": _csrf(client)})
        assert started.status_code == 200
        rejected = client.post(
            "/mfa/setup/verify", data={"code": "wrong", "_csrf_token": _csrf(client)}
        )
        assert rejected.status_code == 400

    locked = client.post("/mfa/setup/start", data={"_csrf_token": _csrf(client)})
    assert locked.status_code == 429


def test_volunteer_login_does_not_require_mfa():
    """The mandatory policy applies only to administrators and treasurers."""
    from club_gift_aid_manager.db import create_person

    person = create_person(name="MFA Volunteer", role="volunteer")
    user = create_user(
        username="mfa-volunteer",
        password=PASSWORD,
        role="volunteer",
        person_id=person["id"],
    )
    client = app.test_client()

    response = _password_login(client, user["username"])

    assert response.status_code == 302
    assert response.location.endswith("/")
    with client.session_transaction() as browser_session:
        assert browser_session["user_id"] == user["id"]


def test_admin_reset_removes_enrollment_and_invalidates_target_sessions():
    """An audited administrator reset forces the target through enrollment again."""
    from club_gift_aid_manager.mfa.service import start_enrollment, verify_totp
    from tests.helpers import signed_in_client

    target = create_user(username="mfa-reset-target", password=PASSWORD, role="treasurer")
    secret = start_enrollment(target["id"])
    verify_totp(target["id"], pyotp.TOTP(secret).now(), enable=True)
    previous_version = get_user_by_id(target["id"])["session_version"]
    admin = signed_in_client("admin")

    response = admin.post(
        f"/admin/users/{target['id']}/mfa-reset",
        headers={"X-CSRF-Token": "test-csrf-token"},
    )

    assert response.status_code == 200
    assert response.get_json() == {"reset": True, "sessions_invalidated": True}
    assert mfa_status(target["id"]) == "absent"
    assert get_user_by_id(target["id"])["session_version"] == previous_version + 1
    assert any(
        event["action"] == "mfa_reset" and event["entity_id"] == str(target["id"])
        for event in list_audit_events()
    )
