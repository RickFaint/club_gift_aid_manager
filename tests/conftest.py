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
    from app import app

    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.sqlite3")
    # Existing endpoint tests assert the worker's final response directly.
    # Dedicated queue tests override this flag to exercise HTTP 202 polling.
    app.config["IMPORTS_SYNCHRONOUS"] = True
    db_module.create_app_database()
