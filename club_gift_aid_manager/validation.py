"""Small, reusable conversions for values received from HTTP requests.

JSON and HTML forms can represent the same value differently.  For example,
JSON may send the Boolean ``true`` while a form sends the text ``"on"``.
Keeping these conversions here gives every endpoint the same rules and the
same predictable validation errors.
"""

from __future__ import annotations

from datetime import date
import math
import re
from typing import Iterable, Mapping, Any


class ValidationError(ValueError):
    """An input error that is safe to report to an API client."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def require_fields(payload: Mapping[str, Any], fields: Iterable[str]) -> None:
    """Raise an error when a required value is absent or only whitespace.

    A string containing spaces looks non-empty to Python, but it is empty from
    a user's point of view.  Stripping only for this check lets callers retain
    significant internal spaces while preventing blank names from being saved.
    """
    missing = [
        field
        for field in fields
        if payload.get(field) is None
        or (isinstance(payload.get(field), str) and not payload.get(field).strip())
    ]
    if missing:
        raise ValidationError(f"Missing required fields: {', '.join(missing)}")


def as_bool(value: Any, default: bool = False) -> bool:
    """Convert recognised HTML/JSON Boolean values and reject everything else."""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    normalised = str(value).strip().lower()
    if normalised in {"true", "1", "yes", "on"}:
        return True
    if normalised in {"false", "0", "no", "off"}:
        return False
    raise ValidationError("value must be a boolean.")


def as_int(value: Any, field_name: str) -> int:
    """Convert a request value to an integer or raise a readable API error."""
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field_name} must be an integer.") from exc


def as_float(value: Any, field_name: str) -> float:
    """Convert a request value to a finite floating-point number.

    Python accepts special strings such as ``NaN`` and ``Infinity`` as floats.
    Those values cannot represent real money or mileage and can poison totals,
    so this shared boundary rejects them before they reach business logic.
    """
    try:
        converted = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field_name} must be a number.") from exc
    if not math.isfinite(converted):
        raise ValidationError(f"{field_name} must be a finite number.")
    return converted


def as_date(value: Any, field_name: str = "date") -> date:
    """Parse an ISO ``YYYY-MM-DD`` value into a :class:`datetime.date`."""
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field_name} must be a valid ISO date.") from exc


def as_non_negative_int(value: Any, field_name: str) -> int:
    """Return an integer that is zero or greater."""
    converted = as_int(value, field_name)
    if converted < 0:
        raise ValidationError(f"{field_name} must be non-negative.")
    return converted


def as_non_negative_float(value: Any, field_name: str) -> float:
    """Return a finite number that is zero or greater."""
    converted = as_float(value, field_name)
    if converted < 0:
        raise ValidationError(f"{field_name} must be non-negative.")
    return converted


def as_optional_date(value: Any, field_name: str) -> str | None:
    """Validate an optional ISO date and return its database-friendly text."""
    if value is None or value == "":
        return None
    return as_date(value, field_name).isoformat()


def as_text(value: Any, field_name: str, *, maximum: int, required: bool = False) -> str | None:
    """Normalise a human-entered text field and enforce a storage limit."""
    if value is None:
        if required:
            raise ValidationError(f"{field_name} is required.")
        return None
    converted = str(value).strip()
    if required and not converted:
        raise ValidationError(f"{field_name} is required.")
    if not converted:
        return None
    if len(converted) > maximum:
        raise ValidationError(f"{field_name} must be {maximum} characters or fewer.")
    return converted


def as_email(value: Any, field_name: str = "email") -> str | None:
    """Validate a practical email shape without pretending to deliver mail."""
    email = as_text(value, field_name, maximum=254)
    if email is None:
        return None
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise ValidationError(f"{field_name} must be a valid email address.")
    return email

