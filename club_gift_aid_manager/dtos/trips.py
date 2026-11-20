"""Trip request, CSV, calculation, and response DTOs."""

from datetime import date
from typing import Literal

from pydantic import Field

from .base import DTO


class TripCreateDTO(DTO):
    """Validate a complete trip-create request after ownership is resolved."""

    person_id: int = Field(gt=0, le=2**63 - 1)
    origin_id: int = Field(gt=0, le=2**63 - 1)
    destination_id: int = Field(gt=0, le=2**63 - 1)
    vehicle_type: Literal["car", "motorcycle", "bicycle"]
    trip_date: date
    return_trip: bool = False
    passengers: int = Field(default=0, ge=0)
    declaration_accepted: bool


class PaymentQuoteDTO(DTO):
    """Validate a staff payment quote request from stored trip identities."""

    person_id: int = Field(gt=0, le=2**63 - 1)
    trip_ids: list[int] = Field(min_length=1, max_length=10000)


class TripCorrectionDTO(DTO):
    """Bound the staff note retained alongside an original paid trip."""

    note: str = Field(min_length=1, max_length=2000)


class TripCSVRowDTO(DTO):
    """Represent one normalized CSV row that has explicitly accepted the declaration."""

    person_id: int = Field(gt=0)
    origin_id: int = Field(gt=0)
    destination_id: int = Field(gt=0)
    trip_date: date
    vehicle_type: Literal["car", "motorcycle", "bicycle"]
    return_trip: bool = False
    passengers: int = Field(default=0, ge=0)
    declaration_accepted: Literal[True]


class TripDTO(DTO):
    """Describe the persisted trip representation returned by repositories and APIs."""

    id: int
    person_id: int
    origin_id: int | None
    destination_id: int | None
    vehicle_type: str
    trip_date: str
    tax_year: str
    miles_one_way: float
    return_trip: bool
    total_miles: float
    passengers: int
    declaration_accepted: bool
    declaration_text: str
    declared_at: str | None
    status: str


class TripCalculationDTO(DTO):
    """Describe a mileage calculation result in miles and integer pence."""

    total_miles: float = Field(ge=0, allow_inf_nan=False)
    mileage_amount_pence: int = Field(ge=0)
    passenger_amount_pence: int = Field(ge=0)
    total_amount_pence: int = Field(ge=0)
    tax_year: str
    rate_effective_from: str | None = None


class RouteDistanceDTO(DTO):
    """Describe the road distance calculated between two saved destinations."""

    origin_id: int
    destination_id: int
    distance_miles: float = Field(ge=0, allow_inf_nan=False)
