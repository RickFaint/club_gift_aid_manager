"""Regression tests for explicit dependencies and operational controls."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import sqlite3

import pytest
from cryptography.fernet import Fernet

from app import app
from backup import create_backup, restore_test, rotate_database_keys
from club_gift_aid_manager import db as db_module
from club_gift_aid_manager.core.logging import JsonFormatter, safe_metadata
from club_gift_aid_manager.factory import parse_trusted_hosts
from club_gift_aid_manager.mfa.service import mfa_status, start_enrollment
from club_gift_aid_manager.security import storage
from club_gift_aid_manager.security.crypto import EncryptedValueError
from monitoring import evaluate_log
from retention import apply_policy


def test_views_have_no_wildcard_imports() -> None:
    """Every view dependency must be reviewable at its import site."""
    views = Path(__file__).resolve().parents[2] / "club_gift_aid_manager" / "views"
    for path in views.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        wildcard_imports = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names)
        ]
        assert not wildcard_imports, f"Wildcard import in {path.name}"


def test_structured_logging_drops_sensitive_and_unknown_metadata() -> None:
    """Credentials, personal fields, and submitted values never reach JSON logs."""
    metadata = safe_metadata(
        {
            "request_id": "safe-id",
            "status_code": 403,
            "password": "never-log-this",
            "email": "person@example.test",
            "payload": {"notes": "private"},
            "unknown": "discarded",
        }
    )
    assert metadata == {"request_id": "safe-id", "status_code": 403}

    record = logging.LogRecord("test", logging.WARNING, __file__, 1, "ignored", (), None)
    record.event_name = "permission_denied"
    record.event_data = metadata
    output = json.loads(JsonFormatter().format(record))
    assert output["event"] == "permission_denied"
    assert output["request_id"] == "safe-id"
    assert "person@example.test" not in json.dumps(output)


def test_request_ids_and_trusted_host_rejection() -> None:
    """Responses are correlated and arbitrary Host headers fail before routing."""
    client = app.test_client()
    response = client.get("/login")
    assert len(response.headers["X-Request-ID"]) == 32
    rejected = client.get("/login", headers={"Host": "attacker.example"})
    assert rejected.status_code == 400


def test_production_requires_explicit_trusted_hosts() -> None:
    """Production cannot silently accept development host defaults."""
    with pytest.raises(RuntimeError, match="TRUSTED_HOSTS"):
        parse_trusted_hosts("production", None)
    assert parse_trusted_hosts("production", "gift-aid.example, admin.gift-aid.example") == [
        "gift-aid.example",
        "admin.gift-aid.example",
    ]


def test_monitoring_thresholds_emit_only_actionable_alerts(tmp_path: Path) -> None:
    """Recent repeated events cross thresholds while old events do not."""
    now = datetime.now(timezone.utc)
    records = [
        {"timestamp": now.isoformat(), "event": "login_throttled"},
        {"timestamp": now.isoformat(), "event": "login_throttled"},
        {"timestamp": now.isoformat(), "event": "login_throttled"},
        {
            "timestamp": (now - timedelta(days=1)).isoformat(),
            "event": "database_integrity_failed",
        },
    ]
    log = tmp_path / "application.jsonl"
    log.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
    assert evaluate_log(log, since=now - timedelta(minutes=15)) == {"login_throttled": 3}


def test_backup_restore_drill_uses_an_isolated_database(tmp_path: Path, monkeypatch) -> None:
    """Audit and MFA ciphertext survives rotation and restore using only the new key."""
    monkeypatch.setattr(storage, "PROJECT_DIRECTORY", tmp_path / "checkout")
    user = db_module.create_user(
        username="restore-admin", password="Secure-Restore-Key3!", role="admin"
    )
    start_enrollment(user["id"])
    db_module.record_audit_event(
        actor_user_id=user["id"],
        actor_username=user["username"],
        action="created",
        entity_type="user",
        entity_id=user["id"],
    )
    old_key = os.environ["GIFT_AID_ENC_KEY"]
    new_key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("GIFT_AID_ENC_KEY", new_key)
    with pytest.raises(EncryptedValueError):
        rotate_database_keys(db_module.DB_PATH)

    # Retrying with the old key must work after a rollback, including the audit trigger check.
    monkeypatch.setenv("GIFT_AID_ENC_KEY", f"{new_key},{old_key}")
    assert rotate_database_keys(db_module.DB_PATH) == 3

    monkeypatch.setenv("GIFT_AID_ENC_KEY", new_key)
    assert mfa_status(user["id"]) == "pending"
    assert db_module.list_audit_events()[0]["actor_username"] == "restore-admin"
    directory = tmp_path / "backups"
    backup, _deleted = create_backup(db_module.DB_PATH, directory, retention_days=30)
    result = restore_test(backup)
    assert result["integrity"] == "ok"
    assert result["decrypted_values"] == 3
    assert result["table_count"] > 0
    assert not list(tmp_path.glob("gift-aid-restore-*"))
    connection = sqlite3.connect(backup)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE audit_events SET actor_username = actor_username")
    finally:
        connection.close()


def test_backup_configuration_rejects_checkout_and_missing_directory(monkeypatch) -> None:
    """Backups require a configured private directory outside the whole checkout."""
    monkeypatch.delenv("GIFT_AID_BACKUP_DIR", raising=False)
    with pytest.raises(RuntimeError, match="GIFT_AID_BACKUP_DIR"):
        create_backup(db_module.DB_PATH)
    with pytest.raises(RuntimeError, match="outside the repository"):
        create_backup(db_module.DB_PATH, Path(__file__).resolve().parents[2] / "backups")


def test_retention_policy_previews_then_deletes_only_recognized_files(tmp_path: Path) -> None:
    """Retention is dry-run by default and ignores unrelated files."""
    logs = tmp_path / "logs"
    backups = tmp_path / "backups"
    logs.mkdir()
    backups.mkdir()
    old_log = logs / "application.jsonl"
    old_backup = backups / "club-gift-aid-20000101T000000000000Z.sqlite3.backup"
    unrelated = logs / "keep.txt"
    for path in (old_log, old_backup, unrelated):
        path.write_text("test", encoding="utf-8")
        old = (datetime.now(timezone.utc) - timedelta(days=100)).timestamp()
        os.utime(path, (old, old))
    policy = {
        "log_directory": str(logs),
        "log_retention_days": 30,
        "backup_directory": str(backups),
        "backup_retention_days": 30,
        "approved_person_ids": [],
    }
    preview = apply_policy(policy)
    assert preview["expired_logs"] == 1
    assert old_log.exists() and old_backup.exists()
    result = apply_policy(policy, apply=True)
    assert result["deleted_logs"] == 1
    assert result["deleted_backups"] == 1
    assert unrelated.exists()
