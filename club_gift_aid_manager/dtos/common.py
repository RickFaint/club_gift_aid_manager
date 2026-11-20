"""DTOs shared across several route domains."""

from typing import Any, Literal

from pydantic import Field

from .base import DTO


class PaginatedDTO(DTO):
    """Define the common envelope returned by paginated collection endpoints."""

    items: list[dict[str, Any]]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=200)
    offset: int = Field(ge=0)


class ImportJobDTO(DTO):
    """Describe the public status of a bounded background CSV import job."""

    job_id: str
    kind: str
    status: Literal["queued", "running", "completed", "failed"]
    result: dict[str, Any] | None = None
    http_status: int | None = None
