"""Create, verify, retain, and key-rotate encrypted SQLite backups."""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

from club_gift_aid_manager.security.crypto import (
    EncryptedValueError,
    decrypt_value,
    rotate_value,
    validate_encryption_config,
)
from club_gift_aid_manager.db import DB_PATH
from club_gift_aid_manager.models import Base, EncryptedText
from club_gift_aid_manager.security.storage import (
    ensure_private_directory,
    ensure_private_file,
    validate_private_path,
)
from club_gift_aid_manager.core.logging import configure_logging, log_event


BACKUP_PATTERN = "club-gift-aid-*.sqlite3.backup"
logger = logging.getLogger(__name__)


def _encrypted_tables(conn: sqlite3.Connection) -> Iterator[tuple[str, str, tuple[str, ...]]]:
    """Describe encrypted columns from the models, allowing older backups without a table."""
    existing = {row[0] for row in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
    for table in Base.metadata.tables.values():
        if table.name not in existing:
            continue
        physical_columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table.name})")}
        fields = tuple(
            column.name
            for column in table.columns
            if isinstance(column.type, EncryptedText) and column.name in physical_columns
        )
        if fields:
            primary_key = tuple(table.primary_key.columns)
            if len(primary_key) != 1:
                raise RuntimeError(f"Key rotation requires a single primary key on {table.name}.")
            yield table.name, primary_key[0].name, fields


def _open_backup_read_only(path: Path) -> sqlite3.Connection:
    """Open the exact database path without creating it or interpreting its filename as URI syntax."""
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def delete_expired_backups(
    directory: Path, retention_days: int, *, now: datetime | None = None
) -> list[Path]:
    """Delete only recognized backups older than the configured retention period."""
    if retention_days < 1:
        raise ValueError("Backup retention must be at least one day.")
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=retention_days)
    deleted = []
    for candidate in directory.glob(BACKUP_PATTERN):
        modified = datetime.fromtimestamp(candidate.stat().st_mtime, timezone.utc)
        if candidate.is_file() and modified < cutoff:
            candidate.unlink()
            deleted.append(candidate)
    return deleted


def create_backup(
    source: Path = DB_PATH,
    directory: Path | None = None,
    retention_days: int = 30,
) -> tuple[Path, list[Path]]:
    """Create a consistent online SQLite backup and apply retention."""
    validate_encryption_config()
    source = source.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Database does not exist: {source}")
    configured = directory if directory is not None else os.environ.get("GIFT_AID_BACKUP_DIR")
    if not configured:
        raise RuntimeError("Set GIFT_AID_BACKUP_DIR or pass --directory.")
    backup_directory = validate_private_path(
        Path(configured), purpose="Backup directory", allow_file=False
    )
    ensure_private_directory(backup_directory)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = backup_directory / f"club-gift-aid-{timestamp}.sqlite3.backup"
    if os.name == "posix":
        descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(descriptor)
    source_conn = _open_backup_read_only(source)
    destination_conn = sqlite3.connect(destination)
    try:
        source_conn.backup(destination_conn)
    finally:
        destination_conn.close()
        source_conn.close()
    ensure_private_file(destination)
    verify_backup(destination)
    deleted = delete_expired_backups(backup_directory, retention_days)
    return destination, deleted


def verify_backup(path: Path) -> dict[str, object]:
    """Check SQLite integrity, relationships, and decryptability of every encrypted value."""
    validate_encryption_config()
    backup_path = path.expanduser().resolve()
    if not backup_path.is_file():
        raise FileNotFoundError(f"Backup does not exist: {backup_path}")
    conn = _open_backup_read_only(backup_path)
    sample = None
    checked = 0
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"Backup integrity check failed: {result}")
        foreign_key_error = conn.execute("PRAGMA foreign_key_check").fetchone()
        if foreign_key_error is not None:
            raise RuntimeError(f"Backup failed foreign-key verification in {foreign_key_error[0]}.")
        for table, _, fields in _encrypted_tables(conn):
            # All identifiers are from the application's model metadata.
            for row in conn.execute(f"SELECT {', '.join(fields)} FROM {table}"):
                for field, value in zip(fields, row, strict=True):
                    if value is None:
                        continue
                    try:
                        decrypt_value(value)
                    except EncryptedValueError as exc:
                        raise RuntimeError(
                            f"Cannot decrypt backup data in {table}.{field}."
                        ) from exc
                    sample = sample or f"{table}.{field}"
                    checked += 1
    finally:
        conn.close()
    return {"integrity": "ok", "decrypted_sample": sample, "decrypted_values": checked}


def restore_test(path: Path) -> dict[str, object]:
    """Restore a backup into an isolated database and verify readability.

    The temporary restored database is deleted before returning. Encryption
    configuration must match the backup, proving both data and keys recover.
    """
    source_path = path.expanduser().resolve()
    verify_backup(source_path)
    with tempfile.TemporaryDirectory(prefix="gift-aid-restore-") as directory:
        restored = Path(directory) / "restored.sqlite3"
        source_conn = _open_backup_read_only(source_path)
        restored_conn = sqlite3.connect(restored)
        try:
            source_conn.backup(restored_conn)
        finally:
            restored_conn.close()
            source_conn.close()
        result = verify_backup(restored)
        conn = _open_backup_read_only(restored)
        try:
            table_count = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'"
            ).fetchone()[0]
        finally:
            conn.close()
        if table_count < 1:
            raise RuntimeError("Restored database has no application tables.")
        return {**result, "table_count": table_count}


def rotate_database_keys(path: Path = DB_PATH) -> int:
    """Rotate every encrypted field to the first configured Fernet key."""
    validate_encryption_config()
    database_path = path.expanduser().resolve()
    if not database_path.is_file():
        raise FileNotFoundError(f"Database does not exist: {database_path}")
    conn = sqlite3.connect(f"{database_path.as_uri()}?mode=rw", uri=True)
    conn.row_factory = sqlite3.Row
    rotated = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        for table, primary_key, fields in _encrypted_tables(conn):
            update_trigger = None
            if table == "audit_events":
                trigger = conn.execute(
                    "SELECT sql FROM sqlite_schema WHERE type='trigger' AND tbl_name='audit_events' "
                    "AND name='audit_events_no_update'"
                ).fetchone()
                if trigger is None or not trigger["sql"]:
                    raise RuntimeError(
                        "The audit update trigger is missing. Repair it before key rotation."
                    )
                update_trigger = trigger["sql"]
                # Suspend updates only on this connection, in the same write transaction.
                # A failure rolls back the DDL and any ciphertext updates together.
                conn.execute("DROP TRIGGER audit_events_no_update")
            rows = conn.execute(f"SELECT {primary_key}, {', '.join(fields)} FROM {table}")
            for row in rows:
                updates = {
                    field: rotate_value(row[field]) for field in fields if row[field] is not None
                }
                if not updates:
                    continue
                assignments = ", ".join(f"{field} = ?" for field in updates)
                conn.execute(
                    f"UPDATE {table} SET {assignments} WHERE {primary_key} = ?",
                    (*updates.values(), row[primary_key]),
                )
                rotated += len(updates)
            if update_trigger is not None:
                conn.execute(update_trigger)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    ensure_private_file(database_path)
    return rotated


def main() -> None:
    """Parse the backup CLI and dispatch create, verify, or key-rotation work."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create_parser = subparsers.add_parser("create", help="Create and retain encrypted backups.")
    create_parser.add_argument("--database", type=Path, default=DB_PATH)
    create_parser.add_argument("--directory", type=Path)
    create_parser.add_argument("--retention-days", type=int, default=30)
    verify_parser = subparsers.add_parser("verify", help="Verify integrity and decryption.")
    verify_parser.add_argument("file", type=Path)
    restore_parser = subparsers.add_parser(
        "restore-test", help="Restore into an isolated database and verify recovery."
    )
    restore_parser.add_argument("file", type=Path)
    rotate_parser = subparsers.add_parser(
        "rotate-keys", help="Re-encrypt data with the newest key."
    )
    rotate_parser.add_argument("--database", type=Path, default=DB_PATH)
    args = parser.parse_args()
    configure_logging(os.environ.get("CLUB_GIFT_AID_LOG_LEVEL", "INFO"))
    try:
        if args.command == "create":
            destination, deleted = create_backup(args.database, args.directory, args.retention_days)
            log_event(
                logger,
                "backup_completed",
                deleted_count=len(deleted),
                outcome="verified",
            )
            print(f"Created and verified {destination}; deleted {len(deleted)} expired backup(s).")
        elif args.command == "verify":
            result = verify_backup(args.file)
            print(
                f"Integrity: {result['integrity']}. Encrypted values verified: {result['decrypted_values']}."
            )
        elif args.command == "restore-test":
            result = restore_test(args.file)
            log_event(logger, "backup_restore_test_completed", outcome="verified")
            print(f"Restore test passed; {result['table_count']} table(s) readable.")
        else:
            print(f"Rotated {rotate_database_keys(args.database)} encrypted value(s).")
    except Exception:
        event = (
            "database_integrity_failed"
            if args.command in {"verify", "restore-test"}
            else "backup_failed"
        )
        log_event(logger, event, level=logging.ERROR, exc_info=True, outcome="failed")
        raise


if __name__ == "__main__":
    main()
