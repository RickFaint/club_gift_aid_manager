"""Disposable, deterministically seeded Flask server for Playwright."""

from __future__ import annotations

import base64
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet


ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / ".playwright"
DB_PATH = RUNTIME / "browser-tests.sqlite3"
sys.path.insert(0, str(ROOT))

shutil.rmtree(RUNTIME, ignore_errors=True)
RUNTIME.mkdir()
os.environ.update(
    CLUB_GIFT_AID_DB=str(DB_PATH),
    CLUB_GIFT_AID_ENV="development",
    CLUB_GIFT_AID_SECRET="browser-test-session-secret-at-least-32-chars",
    GIFT_AID_ENC_KEY=Fernet.generate_key().decode("ascii"),
    GIFT_AID_BLIND_INDEX_KEY=base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"),
)

from club_gift_aid_manager.db import create_destination, create_person, create_user  # noqa: E402
from club_gift_aid_manager.factory import create_app  # noqa: E402
from club_gift_aid_manager.database import unit_of_work  # noqa: E402
from club_gift_aid_manager.models import UserMFA  # noqa: E402


app = create_app()
app.config.update(IMPORTS_SYNCHRONOUS=True)

volunteer = create_person(
    name="Browser Volunteer",
    email="volunteer@example.test",
    role="volunteer",
)
create_person(
    name='<img src=x onerror="window.__tableXss=true">',
    email="escaped@example.test",
    role="other",
)
create_destination(name="Club Ground", address="1 Home Road, London", home_ground=True)
create_destination(name="Away Ground", address="2 Away Road, London")

password = "Browser-Test-Password3!"
mfa_secret = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
staff_users = [create_user(username="browser-admin", password=password, role="admin")]
staff_users.extend(
    create_user(username=f"browser-admin-{number}", password=password, role="admin")
    for number in range(1, 10)
)
staff_users.append(
    create_user(
        username="browser-treasurer", password=password, role="treasurer", person_id=volunteer["id"]
    )
)
create_user(
    username="browser-volunteer", password=password, role="volunteer", person_id=volunteer["id"]
)
with unit_of_work() as database_session:
    database_session.add_all(
        UserMFA(
            user_id=user["id"],
            secret=mfa_secret,
            recovery_code_hashes="[]",
            enabled_at=datetime.now(timezone.utc).isoformat(),
        )
        for user in staff_users
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5173, debug=False, use_reloader=False, threaded=True)
