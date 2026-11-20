"""Central object-level authorization policies.

These predicates are intentionally independent of Flask and persistence.  Endpoint
decorators still provide coarse role access; services and routes use these rules for
ownership and assignment decisions involving individual domain objects.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .roles import Role


def is_volunteer(user: Mapping[str, Any]) -> bool:
    """Return whether the supplied user has the restricted volunteer role."""
    return user.get("role") == Role.VOLUNTEER.value


def can_view_person(user: Mapping[str, Any], person_id: int) -> bool:
    """Staff may view any person; volunteers may view only their linked record."""
    return not is_volunteer(user) or user.get("person_id") == person_id


def can_view_trip(user: Mapping[str, Any], trip: Mapping[str, Any]) -> bool:
    """Staff may view any trip; volunteers may view only trips they own."""
    return can_view_person(user, int(trip["person_id"]))


def can_create_trip_for(user: Mapping[str, Any], person_id: int) -> bool:
    """Administrators may assign trips; other roles use their linked person."""
    return user.get("role") == Role.ADMIN.value or user.get("person_id") == person_id


def trip_scope_person_id(user: Mapping[str, Any]) -> int | None:
    """Return the mandatory query scope for a user, or None for unrestricted staff."""
    return user.get("person_id") if is_volunteer(user) else None


def can_assign_imported_trips(user: Mapping[str, Any]) -> bool:
    """Only administrators may assign imported trips to arbitrary people."""
    return user.get("role") == Role.ADMIN.value
