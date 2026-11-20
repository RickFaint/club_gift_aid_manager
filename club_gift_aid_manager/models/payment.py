"""Reimbursement payment model and its trip association rows."""

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base
from .types import EncryptedText

if TYPE_CHECKING:
    from .donation import DonationPayment
    from .person import Person
    from .trip import Trip


class Payment(Base):
    """Map a reimbursement payment made to one person for complete trips."""

    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id"), nullable=False)
    batch_id: Mapped[str | None] = mapped_column(EncryptedText)
    amount_pence: Mapped[str] = mapped_column(EncryptedText, nullable=False)
    paid_date: Mapped[str | None] = mapped_column(Text)
    method: Mapped[str | None] = mapped_column(EncryptedText)
    reference: Mapped[str | None] = mapped_column(EncryptedText)
    calculation_snapshot: Mapped[str | None] = mapped_column(EncryptedText, deferred=True)
    request_token: Mapped[str | None] = mapped_column(Text, unique=True)

    person: Mapped["Person"] = relationship(back_populates="payments")
    donation_links: Mapped[list["DonationPayment"]] = relationship(back_populates="payment")
    trip_links: Mapped[list["PaymentTrip"]] = relationship(back_populates="payment")


class PaymentTrip(Base):
    """Map the association assigning a trip to its single reimbursement payment."""

    __tablename__ = "payment_trips"
    __table_args__ = (UniqueConstraint("trip_id"),)

    payment_id: Mapped[int] = mapped_column(ForeignKey("payments.id"), primary_key=True)
    trip_id: Mapped[int] = mapped_column(ForeignKey("trips.id"), primary_key=True)

    payment: Mapped[Payment] = relationship(back_populates="trip_links")
    trip: Mapped["Trip"] = relationship(back_populates="payment_links")
