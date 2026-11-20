"""Regression coverage for individually revocable authenticated sessions."""

from app import app
from club_gift_aid_manager.db import create_person, create_user


def test_logout_revokes_a_previously_copied_cookie():
    """Confirm logout invalidates server state, not merely the browser's cookie copy.

    Replaying a cookie captured before logout must therefore fail authentication even
    though its signature and client-side expiry remain otherwise valid.
    """
    person = create_person(name="Session owner", role="volunteer")
    create_user(
        username="session-owner",
        password="Secure-Test-Password1!",
        role="volunteer",
        person_id=person["id"],
    )
    browser = app.test_client()
    browser.get("/login")
    with browser.session_transaction() as browser_session:
        csrf = browser_session["_csrf_token"]
    response = browser.post(
        "/login",
        data={
            "username": "session-owner",
            "password": "Secure-Test-Password1!",
            "_csrf_token": csrf,
        },
    )
    assert response.status_code == 302

    copied_cookie = browser.get_cookie(app.config["SESSION_COOKIE_NAME"])
    copied = app.test_client()
    copied.set_cookie(app.config["SESSION_COOKIE_NAME"], copied_cookie.value)

    with browser.session_transaction() as browser_session:
        logout_csrf = browser_session["_csrf_token"]
    assert browser.post("/logout", data={"_csrf_token": logout_csrf}).status_code == 302
    assert copied.get("/trips/page").headers["Location"].endswith("/login")
