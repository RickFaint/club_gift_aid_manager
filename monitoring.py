"""Evaluate structured JSON logs against simple operational alert thresholds."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path


DEFAULT_THRESHOLDS = {
    "login_throttled": 3,
    "permission_denied": 5,
    "import_failed": 1,
    "backup_failed": 1,
    "database_integrity_failed": 1,
    "routing_service_unavailable": 3,
}


def evaluate_log(path: Path, *, since: datetime, thresholds=None) -> dict[str, int]:
    """Return events whose count reaches the configured threshold."""
    limits = thresholds or DEFAULT_THRESHOLDS
    counts: Counter[str] = Counter()
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                record = json.loads(line)
                timestamp = datetime.fromisoformat(record["timestamp"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
            if timestamp >= since and record.get("event") in limits:
                counts[record["event"]] += 1
    return {event: count for event, count in counts.items() if count >= limits[event]}


def main() -> None:
    """Exit non-zero when alert thresholds are reached."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    parser.add_argument("--window-minutes", type=int, default=15)
    args = parser.parse_args()
    alerts = evaluate_log(
        args.log,
        since=datetime.now(timezone.utc) - timedelta(minutes=args.window_minutes),
    )
    for event, count in sorted(alerts.items()):
        print(f"ALERT {event} count={count}")
    raise SystemExit(1 if alerts else 0)
