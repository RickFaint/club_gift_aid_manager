"""Small, reusable conversions for values received from HTTP requests.

JSON and HTML forms can represent the same value differently.  For example,
JSON may send the Boolean ``true`` while a form sends the text ``"on"``.
Keeping these conversions here gives every endpoint the same rules and the
same predictable validation errors.
"""

from __future__ import annotations

from datetime import date
from typing import Iterable, Mapping, Any


class ValidationError(Exception):
    """An input error that is safe to report to an API client."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def require_fields(payload: Mapping[str, Any], fields: Iterable[str]) -> None:
    """Raise an error listing fields whose value is absent or an empty string."""
    missing = [field for field in fields if payload.get(field) in (None, "")]
    if missing:
        raise ValidationError(f"Missing required fields: {', '.join(missing)}")


def as_bool(value: Any, default: bool = False) -> bool:
    """Convert common HTML/JSON truth values, using ``default`` when omitted."""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "on"}


def as_int(value: Any, field_name: str) -> int:
    """Convert a request value to an integer or raise a readable API error."""
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field_name} must be an integer.") from exc


def as_float(value: Any, field_name: str) -> float:
    """Convert a request value to a floating-point number."""
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field_name} must be a number.") from exc


def as_date(value: Any, field_name: str = "date") -> date:
    """Parse an ISO ``YYYY-MM-DD`` value into a :class:`datetime.date`."""
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field_name} must be a valid ISO date.") from exc

