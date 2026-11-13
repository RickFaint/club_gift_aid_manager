"""Unit tests for the request-validation vocabulary shared by route modules."""

from datetime import date

import pytest

from club_gift_aid_manager.validation import (
    ValidationError,
    as_bool,
    as_date,
    as_float,
    as_int,
    require_fields,
)


def test_validation_error_exposes_public_message_and_status():
    """Routes can safely turn validation metadata into the standard JSON response."""
    error = ValidationError("Invalid request.", status_code=422)

    assert error.message == "Invalid request."
    assert error.status_code == 422


def test_require_fields_preserves_field_order_in_error():
    """Missing-field output remains stable and useful to API clients."""
    with pytest.raises(ValidationError) as caught:
        require_fields({"second": ""}, ("first", "second", "third"))

    assert caught.value.message == "Missing required fields: first, second, third"
    assert caught.value.status_code == 400


def test_require_fields_rejects_whitespace_only_values():
    """Spaces cannot satisfy a required human-readable field."""
    with pytest.raises(ValidationError, match="Missing required fields: name"):
        require_fields({"name": "   "}, ("name",))


@pytest.mark.parametrize("value", [True, "true", "1", "yes", "on", "YES"])
def test_as_bool_recognises_supported_true_values(value):
    """Boolean JSON values and common HTML form spellings behave alike."""
    assert as_bool(value) is True


def test_conversion_helpers_return_typed_values():
    """Numeric and ISO-date input is converted before reaching business services."""
    assert as_bool(None, default=True) is True
    assert as_int("12", "count") == 12
    assert as_float("2.5", "miles") == 2.5
    assert as_date("2026-11-13") == date(2026, 11, 13)


@pytest.mark.parametrize("value", ["banana", 2, [], {}])
def test_as_bool_rejects_unknown_values(value):
    """Typos must not silently change a true/false choice to false."""
    with pytest.raises(ValidationError, match="value must be a boolean"):
        as_bool(value)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_as_float_rejects_non_finite_values(value):
    """Database totals must contain real finite measurements."""
    with pytest.raises(ValidationError, match="miles must be a finite number"):
        as_float(value, "miles")


@pytest.mark.parametrize(
    ("converter", "value", "field_name", "message"),
    [
        (as_int, "one", "count", "count must be an integer."),
        (as_float, "many", "miles", "miles must be a number."),
        (as_date, "13/11/2026", "trip_date", "trip_date must be a valid ISO date."),
    ],
)
def test_conversion_helpers_raise_readable_errors(converter, value, field_name, message):
    """Bad values produce ValidationError rather than leaking Python conversion text."""
    with pytest.raises(ValidationError, match=message.replace(".", r"\.")):
        converter(value, field_name)

