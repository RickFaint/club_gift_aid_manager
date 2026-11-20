"""Small test helpers shared by route-focused test modules.

Helpers are ordinary Python functions called by tests.  Database isolation is
handled separately by the automatic fixture in ``conftest.py``.
"""

import time

from app import app
from club_gift_aid_manager.auth.sessions import register_authenticated_session
from club_gift_aid_manager.db import create_user


def signed_in_client(role="admin", person_id=None):
    """Return a Flask client with a real database user and valid session state.

    Writing the session directly keeps most route tests focused on the endpoint
    under test.  Dedicated authentication tests still exercise the login form.
    """
    client = app.test_client()
    user = create_user(
        username=f"test-{role}-{person_id or 'user'}",
        password="Secure-Test-Password1!",
        role=role,
        person_id=person_id,
    )
    now = int(time.time())
    auth_session_id = register_authenticated_session(user["id"], now=now)
    with client.session_transaction() as browser_session:
        browser_session["user_id"] = user["id"]
        browser_session["session_version"] = user["session_version"]
        browser_session["authenticated_at"] = now
        browser_session["mfa_verified_at"] = now
        browser_session["auth_session_id"] = auth_session_id
        browser_session["_csrf_token"] = "test-csrf-token"
    return client
