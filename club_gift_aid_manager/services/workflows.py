"""Application services for workflows that span validation and persistence.

The services deliberately know nothing about Flask.  Routes supply the authenticated
actor and translate service results/exceptions into HTTP responses; the request unit
of work makes each persistence operation and its audit event one transaction.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ..db import (
    create_donation,
    create_payment,
    create_trip,
    create_trips,
    create_user,
    delete_trip,
    get_person,
    get_trip,
    record_audit_event,
    update_donation,
    update_trip,
    update_user,
)
from ..domain.roles import Role
from ..domain.policies import can_create_trip_for, can_view_trip
from .calculations import calculate_tax_year, validate_declaration


class WorkflowNotFoundError(LookupError):
    """Raised when a workflow target does not exist or must remain concealed."""


class WorkflowConflictError(ValueError):
    """Raised when current persisted state prevents an otherwise valid action."""


def _audit(
    actor: Mapping[str, Any],
    action: str,
    entity_type: str,
    entity_id: object,
    changed_fields: Iterable[str] = (),
) -> None:
    """Record an actor's workflow change without persisting submitted values."""
    record_audit_event(
        actor_user_id=actor["id"],
        actor_username=actor["username"],
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        changed_fields=changed_fields,
    )


class TripService:
    """Enforce trip ownership and coordinate trip persistence workflows."""

    def get(self, trip_id: int, *, actor: Mapping[str, Any]) -> dict[str, Any]:
        """Return a visible trip, concealing missing and unauthorized records alike."""
        trip = get_trip(trip_id)
        if trip is None or not can_view_trip(actor, trip):
            raise WorkflowNotFoundError("Trip not found")
        return trip

    def create(
        self,
        data: Mapping[str, Any],
        *,
        actor: Mapping[str, Any],
        declaration_text: str,
    ) -> dict[str, Any]:
        """Validate ownership and declaration acceptance, then create and audit a trip."""
        validate_declaration(bool(data.get("declaration_accepted")), declaration_text)
        requested_person_id = data.get("person_id")
        if actor["role"] == Role.ADMIN.value:
            person_id = int(requested_person_id)
            if get_person(person_id) is None:
                raise WorkflowNotFoundError("Person not found.")
        else:
            person_id = actor.get("person_id")
            if not person_id:
                raise PermissionError("This account is not linked to a person.")
        if not can_create_trip_for(actor, int(person_id)):
            raise PermissionError("You cannot create a trip for that person.")
        values = dict(data)
        values["person_id"] = person_id
        values.setdefault("tax_year", calculate_tax_year(values["trip_date"]))
        values.setdefault("status", "submitted")
        trip = create_trip(**values, declaration_text=declaration_text)
        _audit(actor, "created", "trip", trip["id"], trip.keys())
        return trip

    def create_many(
        self,
        trips: list[dict[str, Any]],
        *,
        actor: Mapping[str, Any],
        declaration_text: str,
    ) -> list[dict[str, Any]]:
        """Persist an already validated trip batch and audit every created record."""
        created = create_trips(trips, declaration_text=declaration_text)
        for trip in created:
            _audit(actor, "created", "trip", trip["id"], trip.keys())
        return created

    def update(
        self,
        trip_id: int,
        changes: Mapping[str, Any],
        *,
        actor: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Apply trip changes and audit the names of fields that changed."""
        trip = update_trip(trip_id, **changes)
        if trip is None:
            raise WorkflowNotFoundError("Trip not found")
        _audit(actor, "updated", "trip", trip_id, changes.keys())
        return trip

    def delete(self, trip_id: int, *, actor: Mapping[str, Any]) -> None:
        """Delete an uncommitted trip and translate persistence failures for callers."""
        try:
            deleted = delete_trip(trip_id)
        except ValueError as exc:
            raise WorkflowConflictError(str(exc)) from exc
        if not deleted:
            raise WorkflowNotFoundError("Trip not found")
        _audit(actor, "deleted", "trip", trip_id)


class DonationService:
    """Coordinate donation validation, persistence, lifecycle, and auditing."""

    def create(self, data: Mapping[str, Any], *, actor: Mapping[str, Any]) -> dict[str, Any]:
        """Create a donation and audit its non-sensitive field names."""
        donation = create_donation(**data)
        _audit(actor, "created", "donation", donation["id"], donation.keys())
        return donation

    def update(
        self,
        donation_id: int,
        changes: Mapping[str, Any],
        *,
        actor: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Update a donation and classify active-state changes for the audit trail."""
        donation = update_donation(donation_id, **changes)
        if donation is None:
            raise WorkflowNotFoundError("Donation not found.")
        action = (
            "dormant"
            if changes.get("active") is False
            else "reactivated"
            if changes.get("active") is True
            else "updated"
        )
        _audit(actor, action, "donation", donation_id, changes.keys())
        return donation


class PaymentService:
    """Create and audit an all-or-nothing payment/trip reconciliation."""

    def create(self, data: Mapping[str, Any], *, actor: Mapping[str, Any]) -> dict[str, Any]:
        """Create an atomic payment/trip reconciliation and audit the operation."""
        payment = create_payment(**data)
        _audit(
            actor,
            "created",
            "payment",
            payment["id"],
            ("person_id", "amount_pence", "paid_date", "method", "reference", "trip_ids"),
        )
        return payment


class UserAdministrationService:
    """Enforce and audit administrator-managed account lifecycle workflows."""

    def create(self, data: Mapping[str, Any], *, actor: Mapping[str, Any]) -> dict[str, Any]:
        """Create an application user and audit its administrative attributes."""
        user = create_user(**data, must_change_password=True)
        _audit(actor, "created", "user", user["id"], ("username", "role", "person_id"))
        return user

    def update(
        self,
        user_id: int,
        changes: Mapping[str, Any],
        *,
        actor: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Update an account and classify activation changes for the audit trail."""
        user = update_user(user_id, **changes)
        if user is None:
            raise WorkflowNotFoundError("User not found.")
        is_active = changes.get("is_active")
        action = (
            "deactivated"
            if is_active is False
            else "reactivated"
            if is_active is True
            else "updated"
        )
        _audit(actor, action, "user", user_id, changes.keys())
        return user
