"""Pure calculations and validation shared by HTTP handlers and persistence."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any


# Baseline rates used before the first administrator-scheduled rate change.
# Amounts are pounds per mile; passenger allowance is added per passenger-mile.
BASE_RATE_BANDS = {
    "car": {"first_10000": Decimal("0.45"), "over_10000": Decimal("0.25")},
    "motorcycle": {"first_10000": Decimal("0.24")},
    "bicycle": {"first_10000": Decimal("0.20")},
    "passenger": Decimal("0.05"),
}


def rate_schedule_to_bands(schedule: dict[str, Any]) -> dict[str, Any]:
    """Convert a stored rate schedule in pence per mile to calculation bands."""
    # Calculations use decimal pounds; storage and admin input remain whole pence.
    return {
        "car": {
            "first_10000": Decimal(int(schedule["car_first_10000_pence"])) / 100,
            "over_10000": Decimal(int(schedule["car_over_10000_pence"])) / 100,
        },
        "motorcycle": {
            "first_10000": Decimal(int(schedule["motorcycle_pence"])) / 100,
        },
        "bicycle": {
            "first_10000": Decimal(int(schedule["bicycle_pence"])) / 100,
        },
        "passenger": Decimal(int(schedule["passenger_pence"])) / 100,
    }


def calculate_tax_year(value: date) -> str:
    """Map a date to the UK tax year, which begins on 6 April."""
    year = value.year
    if value.month > 4 or (value.month == 4 and value.day >= 6):
        start_year = year
        end_year = year + 1
    else:
        start_year = year - 1
        end_year = year
    return f"{start_year}/{str(end_year)[2:]}"


def calculate_total_miles(miles_one_way: float, return_trip: bool) -> float:
    """Convert a one-way distance into the recorded total claim distance."""
    return float(miles_one_way) * (2 if return_trip else 1)


def calculate_trip_amounts(
    *,
    vehicle_type: str,
    trip_date: date,
    miles_one_way: float,
    return_trip: bool,
    passengers: int = 0,
    total_business_miles: float = 0.0,
    other_org_miles: float = 0.0,
    rate_bands: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Calculate miles and reimbursement components for one recorded trip.

    The car rate switches after the 10,000-mile threshold across the supplied
    business and other-organisation mileage. Decimal arithmetic avoids binary
    floating-point currency errors; the result exposes each amount in pence.
    """
    if rate_bands is None:
        rate_bands = BASE_RATE_BANDS

    total_miles = calculate_total_miles(miles_one_way, return_trip)
    tax_year = calculate_tax_year(trip_date)
    total_business = max(float(total_business_miles), 0.0) + max(float(other_org_miles), 0.0)
    remaining_before_threshold = max(10000.0 - total_business, 0.0)
    miles_at_band1 = min(total_miles, remaining_before_threshold)
    miles_at_band2 = max(total_miles - miles_at_band1, 0.0)

    vehicle_bands = rate_bands.get(vehicle_type)
    if vehicle_bands is None:
        vehicle_bands = BASE_RATE_BANDS.get(vehicle_type, BASE_RATE_BANDS["car"])
    first_rate = Decimal(str(vehicle_bands["first_10000"]))
    over_rate = Decimal(str(vehicle_bands.get("over_10000", first_rate)))

    mileage_amount = (Decimal(str(miles_at_band1)) * first_rate) + (Decimal(str(miles_at_band2)) * over_rate)
    passenger_rate = rate_bands.get("passenger", BASE_RATE_BANDS["passenger"])
    passenger_amount = Decimal(str(passengers)) * Decimal(str(total_miles)) * passenger_rate
    total_amount = mileage_amount + passenger_amount

    def pence(value: Decimal) -> int:
        """Convert pounds to integer pence using conventional half-up rounding."""
        return int((value * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    return {
        "tax_year": tax_year,
        "miles_one_way": float(miles_one_way),
        "total_miles": float(total_miles),
        "miles_at_band1": float(miles_at_band1),
        "miles_at_band2": float(miles_at_band2),
        "passengers": passengers,
        "passenger_amount_pence": pence(passenger_amount),
        "mileage_amount_pence": pence(mileage_amount),
        "total_amount_pence": pence(total_amount),
        "vehicle_rate": {"first_10000": str(first_rate), "over_10000": str(over_rate)},
    }


def validate_declaration(declaration_accepted: bool, declaration_text: str | None = None) -> None:
    """Reject a trip unless the user accepted and was shown declaration text."""
    if not declaration_accepted:
        raise ValueError("Declaration must be accepted before the claim can be submitted.")
    if declaration_text is None or not declaration_text.strip():
        raise ValueError("A declaration snapshot is required.")
