"""Security configuration loaded before application test modules are imported."""

import base64
import os

import pytest

from cryptography.fernet import Fernet


os.environ.setdefault("GIFT_AID_ENC_KEY", Fernet.generate_key().decode("ascii"))
os.environ.setdefault(
    "GIFT_AID_BLIND_INDEX_KEY",
    base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"),
)


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    """Create a fresh database for every test function.

    Pytest automatically runs an ``autouse`` fixture before each test.  Pointing
    the database module at a temporary file prevents one test's records from
    changing the result of another test.
    """
    import club_gift_aid_manager.db as db_module

    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.sqlite3")
    db_module.create_app_database()
