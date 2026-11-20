"""Properties that hold across calculation, date, CSV, and validation inputs."""

from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import io
import math

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from club_gift_aid_manager.core.validation import (
    ValidationError,
    as_date,
    as_non_negative_float,
)
from club_gift_aid_manager.dtos import DestinationCSVRowDTO, TripCSVRowDTO
from club_gift_aid_manager.services.calculations import (
    BASE_RATE_BANDS,
    calculate_tax_year,
    calculate_trip_amounts,
)


# The autouse test fixture migrates a fresh SQLite database before each test.
# On loaded CI workers that setup can distort input-generation health timing,
# even though these property bodies are pure and fast.
settings.register_profile(
    "repository",
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile("repository")


finite_miles = st.floats(min_value=0, max_value=1_000_000, allow_nan=False, allow_infinity=False)


@given(
    miles=finite_miles,
    return_trip=st.booleans(),
    passengers=st.integers(min_value=0, max_value=100),
    vehicle=st.sampled_from(("car", "motorcycle", "bicycle")),
)
def test_trip_amount_components_are_non_negative_and_add_up(
    miles: float, return_trip: bool, passengers: int, vehicle: str
) -> None:
    """Every supported journey produces internally consistent integer pence."""
    result = calculate_trip_amounts(
        vehicle_type=vehicle,
        trip_date=date(2026, 6, 1),
        miles_one_way=miles,
        return_trip=return_trip,
        passengers=passengers,
    )

    assert result["total_miles"] == miles * (2 if return_trip else 1)
    assert result["mileage_amount_pence"] >= 0
    assert result["passenger_amount_pence"] >= 0
    assert result["total_amount_pence"] == (
        result["mileage_amount_pence"] + result["passenger_amount_pence"]
    )


@given(miles=st.decimals(min_value="0", max_value="9999", places=2))
def test_car_amount_below_threshold_uses_decimal_half_up_rounding(miles: Decimal) -> None:
    """Sub-threshold car mileage follows the published rate without float drift."""
    result = calculate_trip_amounts(
        vehicle_type="car",
        trip_date=date(2026, 6, 1),
        miles_one_way=float(miles),
        return_trip=False,
    )
    expected = int(
        (miles * BASE_RATE_BANDS["car"]["first_10000"] * 100).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )
    assert result["mileage_amount_pence"] == expected


@given(value=st.dates(min_value=date(1901, 1, 1), max_value=date(9998, 12, 31)))
def test_tax_year_always_contains_date_and_changes_only_on_6_april(value: date) -> None:
    """Tax-year boundaries consistently follow the UK 6 April transition."""
    start = value.year if (value.month, value.day) >= (4, 6) else value.year - 1
    assert calculate_tax_year(value) == f"{start}/{str(start + 1)[2:]}"


@given(value=st.one_of(st.floats(allow_nan=True, allow_infinity=True), st.text(max_size=40)))
def test_non_negative_float_accepts_only_finite_non_negative_values(value: object) -> None:
    """Numeric validation never admits negative or non-finite mileage."""
    try:
        converted = float(value)
    except (TypeError, ValueError):
        converted = None

    if converted is not None and math.isfinite(converted) and converted >= 0:
        assert as_non_negative_float(value, "miles") == converted
    else:
        with pytest.raises(ValidationError):
            as_non_negative_float(value, "miles")


@given(value=st.text(max_size=30))
def test_date_validation_accepts_exactly_iso_calendar_dates(value: str) -> None:
    """Date validation agrees with Python's ISO calendar parser for arbitrary text."""
    try:
        expected = date.fromisoformat(value)
    except ValueError:
        with pytest.raises(ValidationError):
            as_date(value)
    else:
        assert as_date(value) == expected


@given(
    name=st.text(
        alphabet=st.characters(categories=("L", "N", "P", "S")),
        min_size=1,
        max_size=200,
    )
)
def test_destination_csv_round_trip_preserves_quoted_names(name: str) -> None:
    """CSV quoting and DTO validation preserve arbitrary bounded destination names."""
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=["name", "home_ground"])
    writer.writeheader()
    writer.writerow({"name": name, "home_ground": "false"})
    row = next(csv.DictReader(io.StringIO(output.getvalue(), newline="")))

    parsed = DestinationCSVRowDTO.model_validate(row)
    assert parsed.name == name
    assert parsed.home_ground is False


@given(
    person_id=st.integers(),
    origin_id=st.integers(),
    destination_id=st.integers(),
    passengers=st.integers(),
    accepted=st.booleans(),
)
def test_trip_csv_dto_rejects_invalid_identifiers_and_declarations(
    person_id: int,
    origin_id: int,
    destination_id: int,
    passengers: int,
    accepted: bool,
) -> None:
    """A normalized CSV trip is valid exactly when its constrained fields are valid."""
    payload = {
        "person_id": person_id,
        "origin_id": origin_id,
        "destination_id": destination_id,
        "trip_date": "2026-06-01",
        "vehicle_type": "car",
        "passengers": passengers,
        "declaration_accepted": accepted,
    }
    valid = person_id > 0 and origin_id > 0 and destination_id > 0 and passengers >= 0 and accepted
    if valid:
        assert TripCSVRowDTO.model_validate(payload).declaration_accepted is True
    else:
        with pytest.raises(ValueError):
            TripCSVRowDTO.model_validate(payload)
