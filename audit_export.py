"""Create and verify HMAC-signed exports of the append-only audit history.

Store the signing key separately from the database and exported files.  A
reviewer can then detect any later edit to an exported event or its metadata.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from club_gift_aid_manager.crypto import decrypt_value, validate_encryption_config
from club_gift_aid_manager.db import DB_PATH
from club_gift_aid_manager.storage import ensure_private_directory, ensure_private_file, validate_private_path


def _signing_key() -> bytes:
    """Load a dedicated, sufficiently long key without supplying an unsafe default."""
    value = os.environ.get("GIFT_AID_AUDIT_EXPORT_KEY", "")
    if len(value) < 32:
        raise RuntimeError("GIFT_AID_AUDIT_EXPORT_KEY must contain at least 32 characters.")
    return value.encode("utf-8")


def _canonical_payload(document: dict) -> bytes:
    """Serialize the signed portion identically during export and verification."""
    signed = {"metadata": document["metadata"], "events": document["events"]}
    return json.dumps(signed, sort_keys=True, separators=(",", ":")).encode("utf-8")


def build_export(database: Path = DB_PATH) -> dict:
    """Read audit rows in stable order and return a signed JSON-compatible object."""
    validate_encryption_config()
    database = database.expanduser().resolve()
    conn = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT id, actor_user_id, actor_username, action, entity_type,
                      entity_id, changed_fields, occurred_at
               FROM audit_events ORDER BY id"""
        ).fetchall()
    finally:
        conn.close()
    events = []
    for row in rows:
        event = dict(row)
        event["actor_username"] = decrypt_value(event["actor_username"])
        event["changed_fields"] = json.loads(event["changed_fields"])
        events.append(event)
    document = {
        "metadata": {
            "format": "club-gift-aid-audit-v1",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "event_count": len(events),
            "first_event_id": events[0]["id"] if events else None,
            "last_event_id": events[-1]["id"] if events else None,
        },
        "events": events,
    }
    document["hmac_sha256"] = hmac.new(
        _signing_key(), _canonical_payload(document), hashlib.sha256
    ).hexdigest()
    return document


def verify_export(document: dict) -> bool:
    """Return whether the signature and basic event-count metadata are intact."""
    supplied = str(document.get("hmac_sha256", ""))
    expected = hmac.new(
        _signing_key(), _canonical_payload(document), hashlib.sha256
    ).hexdigest()
    return (
        hmac.compare_digest(supplied, expected)
        and document.get("metadata", {}).get("event_count") == len(document.get("events", []))
    )


def write_export(database: Path, destination: Path) -> Path:
    """Write a signed export only to a protected location outside web/source roots."""
    destination = validate_private_path(destination, purpose="Audit export")
    ensure_private_directory(destination.parent)
    document = build_export(database)
    destination.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    ensure_private_file(destination)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    export_parser = subparsers.add_parser("create")
    export_parser.add_argument("file", type=Path)
    export_parser.add_argument("--database", type=Path, default=DB_PATH)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("file", type=Path)
    args = parser.parse_args()
    if args.command == "create":
        print(f"Created signed audit export {write_export(args.database, args.file)}")
        return
    document = json.loads(args.file.read_text(encoding="utf-8"))
    if not verify_export(document):
        raise SystemExit("Audit export verification failed.")
    print(f"Verified {document['metadata']['event_count']} audit event(s).")


if __name__ == "__main__":
    main()
