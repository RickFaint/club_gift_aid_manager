"""Audit and login-throttling DTOs."""

from typing import Any

from pydantic import Field, field_validator

from .base import DTO


class AuditEventDTO(DTO):
    """Represent an audit event while exposing field names but no changed values."""

    id: int
    actor_user_id: int | None
    actor_username: str
    action: str
    entity_type: str
    entity_id: str
    changed_fields: list[str]
    occurred_at: str

    @field_validator("changed_fields", mode="before")
    @classmethod
    def decode_changed_fields(cls, value: Any) -> Any:
        """Convert the database JSON string into the API's list representation."""
        if isinstance(value, str):
            import json

            return json.loads(value)
        return value


class LoginAttemptDTO(DTO):
    """Represent a pseudonymous persistent login-throttling counter."""

    attempt_key: str
    failure_count: int = Field(ge=0)
    window_started_at: int
    locked_until: int
