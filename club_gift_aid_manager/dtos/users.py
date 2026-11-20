"""Request and credential-safe response DTOs for accounts."""

from typing import Literal

from pydantic import Field

from .base import DTO


class UserCreateDTO(DTO):
    """Validate the administrator-supplied fields used to create an account."""

    username: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=15, max_length=1024)
    role: Literal["admin", "treasurer", "volunteer"]
    person_id: int | None = Field(default=None, gt=0)


class UserUpdateDTO(DTO):
    """Validate the optional role, person link, and activation account changes."""

    role: Literal["admin", "treasurer", "volunteer"] | None = None
    person_id: int | None = Field(default=None, gt=0)
    is_active: bool | None = None


class UserImportDTO(DTO):
    """Allow invitation-based provisioning without a password column."""

    username: str = Field(min_length=1, max_length=256)
    password: str | None = Field(default=None, min_length=15, max_length=1024)
    role: Literal["admin", "treasurer", "volunteer"]
    person_id: int | None = Field(default=None, gt=0)


class UserPasswordResetDTO(DTO):
    """Validate the bounded temporary password selected by an administrator."""

    temporary_password: str = Field(min_length=15, max_length=1024)


class UserDTO(DTO):
    """Expose account metadata to callers without ever including password hashes."""

    id: int
    username: str
    role: str
    person_id: int | None
    is_active: bool
    session_version: int
    must_change_password: bool
    created_at: str
