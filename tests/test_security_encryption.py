"""Field encryption, fail-closed startup, and encrypted backup tests."""

from __future__ import annotations

import base64
import os
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

import club_gift_aid_manager.db as db_module
import club_gift_aid_manager.storage as storage_module
from backup import create_backup, delete_expired_backups, verify_backup
from club_gift_aid_manager.crypto import (
    EncryptedValueError,
    decrypt_value,
    encrypt_value,
)
from club_gift_aid_manager.db import (
    create_app_database,
    create_declaration,
    create_destination,
    create_donation,
    create_payment,
    create_person,
    create_trip,
    create_user,
    get_person,
)


@pytest.fixture(autouse=True)
def encrypted_database(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "encrypted.sqlite3")
    create_app_database()


@pytest.mark.parametrize("value", ["Hello, ä¸–ç•Œ ðŸ‘‹", "", None])
def test_encrypt_decrypt_round_trip(value):
    encrypted = encrypt_value(value)
    assert decrypt_value(encrypted) == value
    if value is not None:
        assert encrypted.startswith("enc:v1:")
        if value:
            assert value not in encrypted


def test_plaintext_in_encrypted_column_is_rejected():
    person = create_person(name="Encrypted Person")
    conn = sqlite3.connect(db_module.DB_PATH)
    conn.execute("UPDATE people SET name = 'unexpected plaintext' WHERE id = ?", (person["id"],))
    conn.commit()
    conn.close()
    with pytest.raises(EncryptedValueError, match="unmarked plaintext"):
        get_person(person["id"])


def test_multifernet_rotation_uses_newest_key(monkeypatch):
    old_key = Fernet.generate_key()
    new_key = Fernet.generate_key()
    monkeypatch.setenv("GIFT_AID_ENC_KEY", old_key.decode("ascii"))
    old_token = encrypt_value("rotate me")
    monkeypatch.setenv(
        "GIFT_AID_ENC_KEY", f"{new_key.decode('ascii')},{old_key.decode('ascii')}"
    )
    assert decrypt_value(old_token) == "rotate me"
    new_token = encrypt_value("new value")
    raw_token = new_token.removeprefix("enc:v1:").encode("ascii")
    assert Fernet(new_key).decrypt(raw_token) == b"new value"


def test_production_startup_fails_without_encryption_key(tmp_path):
    environment = os.environ.copy()
    environment.pop("GIFT_AID_ENC_KEY", None)
    environment["CLUB_GIFT_AID_ENV"] = "production"
    environment["CLUB_GIFT_AID_SECRET"] = "s" * 48
    environment["CLUB_GIFT_AID_COOKIE_SECURE"] = "true"
    environment["CLUB_GIFT_AID_DB"] = str(tmp_path / "production.sqlite3")
    result = subprocess.run(
        [sys.executable, "-c", "import app"],
        cwd=Path(__file__).resolve().parent.parent,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "GIFT_AID_ENC_KEY" in result.stderr


def test_raw_database_contains_no_sensitive_plaintext():
    values = [
        "Unicode Person Î©", "private-unique@example.test", "+44 7700 900999",
        "Private notes 314159", "login-unique-user", "Gift Aid Full Name",
        "91 Secret Avenue", "ZZ9 9ZZ", "signed-evidence-unique", "past-and-future-unique",
        "Private Ground Unique", "92 Hidden Road", "Secret Town Unique", "Ground notes unique",
        "car-private-marker", "98765.4", "33333333", "Unique declaration wording 271828",
        "987654321", "wire-transfer-unique", "donation-reference-unique",
        "bank-reference-unique", "7654321", "payment-reference-unique", "8765432",
    ]
    person = create_person(name=values[0], email=values[1], phone=values[2], notes=values[3])
    create_user(username=values[4], password="a-secure-test-password", role="volunteer", person_id=person["id"])
    declaration = create_declaration(
        person_id=person["id"], full_name=values[5], home_address=values[6],
        postcode=values[7], evidence_ref=values[8], scope=values[9],
    )
    destination = create_destination(
        name=values[10], address=values[11], town_city=values[12], postcode="XY1 2AB",
        notes=values[13],
    )
    create_trip(
        person_id=person["id"], origin_id=destination["id"], destination_id=destination["id"],
        vehicle_type=values[14], trip_date=date(2026, 1, 2), miles_one_way=float(values[15]),
        return_trip=False, passengers=int(values[16]), declaration_text=values[17],
    )
    create_donation(
        person_id=person["id"], amount_pence=int(values[18]), method=values[19],
        reference=values[20], declaration_id=declaration["id"], bank_reference=values[21],
        receipt_id=int(values[22]),
    )
    create_payment(
        person_id=person["id"], amount_pence=int(values[18]), method=values[19],
        reference=values[23], batch_id=int(values[24]),
    )
    raw_file = db_module.DB_PATH.read_bytes()
    for value in values:
        assert value.encode("utf-8") not in raw_file
    conn = sqlite3.connect(db_module.DB_PATH)
    for table, fields in db_module.ENCRYPTED_FIELDS.items():
        columns = ", ".join(fields)
        for row in conn.execute(f"SELECT {columns} FROM {table}").fetchall():
            assert all(value is None or str(value).startswith("enc:v1:") for value in row)
    conn.close()


def test_backup_retention_and_verify(tmp_path, monkeypatch):
    person = create_person(name="Backup Sample Person")
    create_donation(person_id=person["id"], amount_pence=43210)
    monkeypatch.setattr(storage_module, "PROJECT_DIRECTORY", tmp_path / "different-project")
    backup_directory = tmp_path / "protected-backups"
    backup_path, deleted = create_backup(db_module.DB_PATH, backup_directory, retention_days=30)
    assert backup_path.is_file()
    assert deleted == []
    assert verify_backup(backup_path)["integrity"] == "ok"
    assert b"Backup Sample Person" not in backup_path.read_bytes()
    old_backup = backup_directory / "club-gift-aid-20000101T000000000000Z.sqlite3.backup"
    old_backup.write_bytes(b"expired")
    old_time = (datetime.now(timezone.utc) - timedelta(days=31)).timestamp()
    os.utime(old_backup, (old_time, old_time))
    removed = delete_expired_backups(backup_directory, 30)
    assert removed == [old_backup]
    assert not old_backup.exists()

