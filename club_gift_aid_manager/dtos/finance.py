"""DTOs for declarations, donations, payments, and rate schedules."""

from datetime import date
from typing import Any

from pydantic import Field

from .base import DTO


class DeclarationCreateDTO(DTO):
    """Validate the evidence and notice linkage for a Gift Aid declaration."""

    person_id: int = Field(gt=0)
    full_name: str = Field(min_length=1, max_length=200)
    home_address: str | None = Field(default=None, max_length=500)
    postcode: str | None = Field(default=None, max_length=20)
    declaration_date: date | None = None
    scope: str = Field(default="future", max_length=50)
    withdrawn_date: date | None = None
    evidence_ref: str | None = Field(default=None, max_length=500)
    evidence_image_base64: str | None = Field(default=None, max_length=1_000_000)
    evidence_image_mime: str | None = Field(default=None, max_length=100)
    privacy_notice_version_id: int | None = Field(default=None, gt=0)
    privacy_notice_sha256: str | None = None


class DeclarationDTO(DTO):
    """Represent a stored declaration, including withdrawal and activity state."""

    id: int
    person_id: int
    full_name: str
    home_address: str | None
    postcode: str | None
    declaration_date: str | None
    scope: str | None
    withdrawn_date: str | None
    evidence_ref: str | None
    has_evidence_image: bool = False
    privacy_notice_version_id: int
    privacy_notice_sha256: str
    active: bool | None = None


class DonationCreateDTO(DTO):
    """Validate a donation and its optional declaration and payment references."""

    person_id: int = Field(gt=0)
    amount_pence: int = Field(gt=0)
    donated_date: date | None = None
    method: str | None = Field(default=None, max_length=100)
    reference: str | None = Field(default=None, max_length=500)
    declaration_id: int | None = Field(default=None, gt=0)
    gift_aid_eligible: bool = False
    gift_aid_claimed_date: date | None = None
    linked_payment_id: int | None = Field(default=None, gt=0)
    linked_payment_ids: list[int] | None = None
    received_date: date | None = None
    bank_reference: str | None = Field(default=None, max_length=500)
    receipt_id: int | None = Field(default=None, gt=0)


class DonationDTO(DTO):
    """Represent a stored donation with all resolved payment-link identifiers."""

    id: int
    person_id: int
    amount_pence: int
    donated_date: str | None
    method: str | None
    reference: str | None
    declaration_id: int | None
    gift_aid_eligible: bool
    gift_aid_claimed_date: str | None
    linked_payment_id: int | None
    linked_payment_ids: list[int] = Field(default_factory=list)
    received_date: str | None
    bank_reference: str | None
    receipt_id: int | None
    active: bool | None = None


class PaymentCreateDTO(DTO):
    """Validate a reimbursement payment and the complete trips it settles."""

    person_id: int = Field(gt=0)
    amount_pence: int = Field(gt=0)
    paid_date: date | None = None
    method: str = Field(default="bank transfer", min_length=1, max_length=100)
    reference: str | None = Field(default=None, max_length=500)
    batch_id: str | None = Field(default=None, max_length=200)
    trip_ids: list[int] = Field(default_factory=list)


class PaymentDTO(DTO):
    """Represent a payment with linked trips and related donation information."""

    id: int
    person_id: int
    batch_id: int | str | None
    amount_pence: int
    paid_date: str | None
    method: str | None
    reference: str | None
    trip_ids: list[int] = Field(default_factory=list)
    trips: list[dict[str, Any]] = Field(default_factory=list)
    linked_donation_total_pence: int | None = None


class MileageRateScheduleDTO(DTO):
    """Represent one effective-dated set of whole-pence mileage rates."""

    effective_from: str
    effective_to: str | None = None
    car_first_10000_pence: int = Field(ge=0)
    car_over_10000_pence: int = Field(ge=0)
    motorcycle_pence: int = Field(ge=0)
    bicycle_pence: int = Field(ge=0)
    passenger_pence: int = Field(ge=0)
    created_at: str | None = None


class DonationPaymentDTO(DTO):
    """Represent the composite key linking a donation to a payment for reference."""

    donation_id: int
    payment_id: int


class PaymentTripDTO(DTO):
    """Represent the composite key assigning a complete trip to one payment."""

    payment_id: int
    trip_id: int
