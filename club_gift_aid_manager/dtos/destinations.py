"""HTTP and CSV DTOs for club locations."""

from pydantic import Field

from .base import DTO


class DestinationCreateDTO(DTO):
    """Validate a new club ground or opponent destination."""

    name: str = Field(min_length=1, max_length=200)
    address: str | None = Field(default=None, max_length=500)
    town_city: str | None = Field(default=None, max_length=200)
    postcode: str | None = Field(default=None, max_length=20)
    notes: str | None = Field(default=None, max_length=2000)
    home_ground: bool = False


class DestinationUpdateDTO(DTO):
    """Validate mutable destination fields supplied by a partial update."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    address: str | None = Field(default=None, max_length=500)
    town_city: str | None = Field(default=None, max_length=200)
    postcode: str | None = Field(default=None, max_length=20)
    notes: str | None = Field(default=None, max_length=2000)
    active: bool | None = None
    home_ground: bool | None = None


class DestinationDTO(DestinationCreateDTO):
    """Represent a stored destination, including activity and cached coordinates."""

    id: int
    active: bool = True
    latitude: float | None = None
    longitude: float | None = None


class DestinationCSVRowDTO(DestinationCreateDTO):
    """The CSV row has the same writable fields as a create request."""
