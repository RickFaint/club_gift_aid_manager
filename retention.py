"""Apply an operator-approved retention policy to bounded data categories."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any

from backup import delete_expired_backups
from club_gift_aid_manager.core.logging import configure_logging, log_event
from club_gift_aid_manager.services.imports import purge_completed_imports

import logging


logger = logging.getLogger(__name__)


def expired_log_files(directory: Path, retention_days: int, *, now=None) -> list[Path]:
    """List recognized JSONL operational logs older than the policy cutoff."""
    if retention_days < 1:
        raise ValueError("Log retention must be at least one day.")
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=retention_days)
    return [
        path
        for path in directory.glob("*.jsonl")
        if path.is_file() and datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < cutoff
    ]


def load_policy(path: Path) -> dict[str, Any]:
    """Load a retention policy and reject unknown top-level fields."""
    policy = json.loads(path.read_text(encoding="utf-8"))
    allowed = {
        "log_directory",
        "log_retention_days",
        "backup_directory",
        "backup_retention_days",
        "approved_person_ids",
    }
    unknown = set(policy) - allowed
    if unknown:
        raise ValueError(f"Unknown retention policy field(s): {', '.join(sorted(unknown))}")
    return policy


def apply_policy(policy: dict[str, Any], *, apply: bool = False) -> dict[str, int]:
    """Preview or apply the approved policy without deleting audit records."""
    logs = expired_log_files(Path(policy["log_directory"]), int(policy["log_retention_days"]))
    backups = list(Path(policy["backup_directory"]).glob("club-gift-aid-*.sqlite3.backup"))
    approved_people = [int(value) for value in policy.get("approved_person_ids", [])]
    deleted_logs = deleted_backups = anonymized_people = 0
    purged_imports = 0
    if apply:
        for path in logs:
            path.unlink()
            deleted_logs += 1
        deleted_backups = len(
            delete_expired_backups(
                Path(policy["backup_directory"]), int(policy["backup_retention_days"])
            )
        )
        purged_imports = purge_completed_imports()
        if approved_people:
            from club_gift_aid_manager.db import anonymize_person

            for person_id in approved_people:
                anonymize_person(person_id)
                anonymized_people += 1
    result = {
        "expired_logs": len(logs),
        "recognized_backups": len(backups),
        "approved_people": len(approved_people),
        "deleted_logs": deleted_logs,
        "deleted_backups": deleted_backups,
        "purged_imports": purged_imports,
        "anonymized_people": anonymized_people,
    }
    log_event(
        logger,
        "retention_job_completed",
        outcome="applied" if apply else "preview",
        deleted_count=deleted_logs + deleted_backups + purged_imports + anonymized_people,
        retained_count=max(len(backups) - deleted_backups, 0),
    )
    return result


def main() -> None:
    """Preview by default and require an explicit flag before deletion."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("policy", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    configure_logging()
    result = apply_policy(load_policy(args.policy), apply=args.apply)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
