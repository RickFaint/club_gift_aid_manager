"""Request and response DTOs for people records."""

import re

from pydantic import Field, field_validator

from .base import DTO


class PersonCreateDTO(DTO):
    """Validate contact and status fields accepted when creating a person."""

    name: str = Field(min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=50)
    role: str = Field(default="volunteer", max_length=50)
    active: bool = True
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("email")
    @classmethod
    def validate_email_shape(cls, value: str | None) -> str | None:
        """Reject obvious non-email text while allowing an omitted address."""
        if value is not None and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("must be a valid email address")
        return value

    @field_validator("phone")
    @classmethod
    def validate_phone_digits(cls, value: str | None) -> str | None:
        """Keep optional phone identifiers as strings but accept digits only."""
        if value is not None and not value.isdigit():
            raise ValueError("must contain numbers only")
        return value


class PersonUpdateDTO(DTO):
    """Validate a partial person update; omitted fields remain unchanged."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=50)
    role: str | None = Field(default=None, max_length=50)
    active: bool | None = None
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("phone")
    @classmethod
    def validate_phone_digits(cls, value: str | None) -> str | None:
        """Apply the same digits-only rule to partial updates."""
        if value is not None and not value.isdigit():
            raise ValueError("must contain numbers only")
        return value


class PersonDTO(PersonCreateDTO):
    """Add the database identity to the validated person fields returned by APIs."""

    id: int
