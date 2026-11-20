"""Structured operational logging with a deliberately small safe metadata surface."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import sys
from typing import Any, Mapping


SAFE_FIELDS = frozenset(
    {
        "request_id",
        "method",
        "path",
        "status_code",
        "role",
        "user_id",
        "job_id",
        "kind",
        "duration_ms",
        "outcome",
        "failure_count",
        "deleted_count",
        "retained_count",
    }
)
SENSITIVE_FRAGMENTS = (
    "password",
    "secret",
    "token",
    "key",
    "email",
    "phone",
    "address",
    "name",
    "value",
    "payload",
    "form",
    "body",
    "cookie",
)


def safe_metadata(values: Mapping[str, Any]) -> dict[str, Any]:
    """Return only allowlisted scalar operational metadata.

    Values submitted by users are intentionally not serialised. Unknown keys,
    sensitive-looking keys, collections, and arbitrary objects are discarded.
    """
    result: dict[str, Any] = {}
    for key, value in values.items():
        normalized = key.casefold()
        if key not in SAFE_FIELDS or any(
            fragment in normalized for fragment in SENSITIVE_FRAGMENTS
        ):
            continue
        if value is None or isinstance(value, (bool, int, float, str)):
            result[key] = value
    return result


class JsonFormatter(logging.Formatter):
    """Render one event per line for log aggregation and alert rules."""

    def format(self, record: logging.LogRecord) -> str:
        """Serialize a log record as one compact, UTC-timestamped JSON object."""
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "event": getattr(record, "event_name", "application_log"),
            "logger": record.name,
        }
        payload.update(safe_metadata(getattr(record, "event_data", {})))
        if record.exc_info:
            payload["exception_type"] = record.exc_info[0].__name__
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def configure_logging(level: str = "INFO") -> None:
    """Configure the application root logger once with JSON output."""
    root = logging.getLogger()
    handler = next((item for item in root.handlers if getattr(item, "_gift_aid_json", False)), None)
    if handler is None:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        handler._gift_aid_json = True  # type: ignore[attr-defined]
        root.handlers.clear()
        root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


def log_event(
    logger: logging.Logger,
    event_name: str,
    *,
    level: int = logging.INFO,
    exc_info: bool = False,
    **metadata: Any,
) -> None:
    """Emit a named event containing only safe operational metadata."""
    logger.log(
        level,
        event_name,
        extra={"event_name": event_name, "event_data": safe_metadata(metadata)},
        exc_info=exc_info,
    )
