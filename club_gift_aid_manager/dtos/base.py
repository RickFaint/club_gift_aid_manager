"""Shared Pydantic configuration for application DTOs."""

from pydantic import BaseModel, ConfigDict


class DTO(BaseModel):
    """Reject unexpected fields and support validation from ORM attributes."""

    model_config = ConfigDict(from_attributes=True, extra="forbid", str_strip_whitespace=True)
