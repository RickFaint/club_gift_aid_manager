"""Administrator-assisted password reset and forced replacement tests."""

import pytest

from app import app
from club_gift_aid_manager.db import create_person, create_user, get_user_by_id, list_audit_events
from tests.helpers import signed_in_client


TEMPORARY_PASSWORD = "Temporary-Access9!"
NEW_PASSWORD = "Replacement-Access8!"


def _login(client, username: str, password: str):
    """Submit credentials with the login page's session CSRF token."""
    client.get("/login")
    with client.session_transaction() as browser_session:
        token = browser_session["_csrf_token"]
    return client.post(
        "/login",
        data={"username": username, "password": password, "_csrf_token": token},
    )


def test_password_policy_applies_to_account_creation():
    """Length alone is insufficient without every required character class."""
    with pytest.raises(ValueError, match="uppercase"):
        create_user(username="weak-password", password="lowercase-number1!", role="admin")
    with pytest.raises(ValueError, match="number"):
        create_user(username="weak-password", password="Uppercase-Symbol!", role="admin")


def test_admin_reset_forces_replacement_and_invalidates_sessions():
    """A reset is audited, revokes sessions, and grants no access with the temporary password."""
    person = create_person(name="Reset Volunteer", role="volunteer")
    target = create_user(
        username="reset-volunteer",
        password="Original-Password7!",
        role="volunteer",
        person_id=person["id"],
    )
    target_client = app.test_client()
    with target_client.session_transaction() as browser_session:
        browser_session["user_id"] = target["id"]
        browser_session["session_version"] = target["session_version"]
        browser_session["_csrf_token"] = "target-csrf"
    admin = signed_in_client("admin")

    reset = admin.post(
        f"/admin/users/{target['id']}/password-reset",
        json={"temporary_password": TEMPORARY_PASSWORD},
        headers={"X-CSRF-Token": "test-csrf-token"},
    )

    assert reset.status_code == 200
    assert reset.get_json() == {
        "reset": True,
        "sessions_invalidated": True,
        "change_required": True,
    }
    assert target_client.get("/people").status_code == 401
    updated = get_user_by_id(target["id"])
    assert updated["must_change_password"] is True
    assert updated["session_version"] == target["session_version"] + 1
    event = next(
        event for event in list_audit_events() if event["action"] == "password_reset_by_admin"
    )
    assert "Temporary-Access9!" not in str(event)


def test_temporary_password_session_can_only_replace_password():
    """The temporary credential leads to a restricted form and rejects reuse or weak values."""
    person = create_person(name="Change Volunteer", role="volunteer")
    target = create_user(
        username="change-volunteer",
        password="Original-Password7!",
        role="volunteer",
        person_id=person["id"],
    )
    admin = signed_in_client("admin")
    admin.post(
        f"/admin/users/{target['id']}/password-reset",
        json={"temporary_password": TEMPORARY_PASSWORD},
        headers={"X-CSRF-Token": "test-csrf-token"},
    )
    client = app.test_client()

    login = _login(client, target["username"], TEMPORARY_PASSWORD)
    assert login.location.endswith("/account/change-temporary-password")
    assert client.get("/people").status_code == 401
    with client.session_transaction() as browser_session:
        token = browser_session["_csrf_token"]
    reused = client.post(
        "/account/change-temporary-password",
        data={
            "new_password": TEMPORARY_PASSWORD,
            "confirm_password": TEMPORARY_PASSWORD,
            "_csrf_token": token,
        },
    )
    assert reused.status_code == 400
    weak = client.post(
        "/account/change-temporary-password",
        data={
            "new_password": "alllowercase123!",
            "confirm_password": "alllowercase123!",
            "_csrf_token": token,
        },
    )
    assert weak.status_code == 400
    changed = client.post(
        "/account/change-temporary-password",
        data={"new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD, "_csrf_token": token},
    )
    assert changed.status_code == 302
    assert changed.location.endswith("/")
    assert get_user_by_id(target["id"])["must_change_password"] is False
    assert client.get("/people").status_code == 200


def test_staff_password_replacement_continues_to_mfa():
    """Replacing a staff temporary password does not bypass mandatory MFA."""
    target = create_user(
        username="reset-treasurer",
        password="Original-Password7!",
        role="treasurer",
    )
    admin = signed_in_client("admin")
    admin.post(
        f"/admin/users/{target['id']}/password-reset",
        json={"temporary_password": TEMPORARY_PASSWORD},
        headers={"X-CSRF-Token": "test-csrf-token"},
    )
    client = app.test_client()
    _login(client, target["username"], TEMPORARY_PASSWORD)
    with client.session_transaction() as browser_session:
        token = browser_session["_csrf_token"]

    changed = client.post(
        "/account/change-temporary-password",
        data={"new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD, "_csrf_token": token},
    )

    assert changed.status_code == 302
    assert changed.location.endswith("/mfa/setup")
    with client.session_transaction() as browser_session:
        assert "user_id" not in browser_session
        assert browser_session["mfa_pending_user_id"] == target["id"]
