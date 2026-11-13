"""Create, verify, retain, and key-rotate encrypted SQLite backups."""

from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from club_gift_aid_manager.crypto import decrypt_value, rotate_value, validate_encryption_config
from club_gift_aid_manager.db import DB_PATH, ENCRYPTED_FIELDS
from club_gift_aid_manager.storage import (
    ensure_private_directory,
    ensure_private_file,
    validate_private_path,
)


BACKUP_PATTERN = "club-gift-aid-*.sqlite3.backup"


def delete_expired_backups(directory: Path, retention_days: int, *, now: datetime | None = None) -> list[Path]:
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
    configured = directory or Path(os.environ.get("GIFT_AID_BACKUP_DIR", ""))
    if not str(configured):
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
    source_conn = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
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
    """Check SQLite integrity and decrypt a sample sensitive value when present."""
    validate_encryption_config()
    backup_path = path.expanduser().resolve()
    if not backup_path.is_file():
        raise FileNotFoundError(f"Backup does not exist: {backup_path}")
    conn = sqlite3.connect(f"file:{backup_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    sample = None
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"Backup integrity check failed: {result}")
        tables = {
            row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        for table, fields in ENCRYPTED_FIELDS.items():
            if table not in tables:
                continue
            for field in fields:
                row = conn.execute(
                    f"SELECT {field} FROM {table} WHERE {field} IS NOT NULL LIMIT 1"
                ).fetchone()
                if row is not None:
                    decrypt_value(row[field])
                    sample = f"{table}.{field}"
                    break
            if sample:
                break
    finally:
        conn.close()
    return {"integrity": "ok", "decrypted_sample": sample}


def rotate_database_keys(path: Path = DB_PATH) -> int:
    """Rotate every encrypted field to the first configured Fernet key."""
    validate_encryption_config()
    database_path = path.expanduser().resolve()
    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row
    rotated = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        for table, fields in ENCRYPTED_FIELDS.items():
            rows = conn.execute(f"SELECT id, {', '.join(fields)} FROM {table}").fetchall()
            for row in rows:
                updates = {field: rotate_value(row[field]) for field in fields if row[field] is not None}
                if not updates:
                    continue
                assignments = ", ".join(f"{field} = ?" for field in updates)
                conn.execute(
                    f"UPDATE {table} SET {assignments} WHERE id = ?",
                    (*updates.values(), row["id"]),
                )
                rotated += len(updates)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    ensure_private_file(database_path)
    return rotated


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create_parser = subparsers.add_parser("create", help="Create and retain encrypted backups.")
    create_parser.add_argument("--database", type=Path, default=DB_PATH)
    create_parser.add_argument("--directory", type=Path)
    create_parser.add_argument("--retention-days", type=int, default=30)
    verify_parser = subparsers.add_parser("verify", help="Verify integrity and decryption.")
    verify_parser.add_argument("file", type=Path)
    rotate_parser = subparsers.add_parser("rotate-keys", help="Re-encrypt data with the newest key.")
    rotate_parser.add_argument("--database", type=Path, default=DB_PATH)
    args = parser.parse_args()
    if args.command == "create":
        destination, deleted = create_backup(args.database, args.directory, args.retention_days)
        print(f"Created and verified {destination}; deleted {len(deleted)} expired backup(s).")
    elif args.command == "verify":
        result = verify_backup(args.file)
        print(f"Integrity: {result['integrity']}; decrypted sample: {result['decrypted_sample'] or 'no data rows'}")
    else:
        print(f"Rotated {rotate_database_keys(args.database)} encrypted value(s).")


if __name__ == "__main__":
    main()
