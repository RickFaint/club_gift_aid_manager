"""Donation model and its payment association rows."""

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base
from .types import EncryptedText

if TYPE_CHECKING:
    from .declaration import GiftAidDeclaration
    from .payment import Payment
    from .person import Person


class Donation(Base):
    """Map a donation, its Gift Aid evidence, and optional payment references."""

    __tablename__ = "donations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id"), nullable=False)
    amount_pence: Mapped[str] = mapped_column(EncryptedText, nullable=False)
    donated_date: Mapped[str | None] = mapped_column(Text)
    method: Mapped[str | None] = mapped_column(EncryptedText)
    reference: Mapped[str | None] = mapped_column(EncryptedText)
    declaration_id: Mapped[int | None] = mapped_column(ForeignKey("gift_aid_declarations.id"))
    gift_aid_eligible: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    gift_aid_claimed_date: Mapped[str | None] = mapped_column(Text)
    linked_payment_id: Mapped[int | None] = mapped_column(Integer)
    received_date: Mapped[str | None] = mapped_column(Text)
    bank_reference: Mapped[str | None] = mapped_column(EncryptedText)
    receipt_id: Mapped[str | None] = mapped_column(EncryptedText)
    active: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )

    person: Mapped["Person"] = relationship(back_populates="donations")
    declaration: Mapped["GiftAidDeclaration | None"] = relationship(back_populates="donations")
    payment_links: Mapped[list["DonationPayment"]] = relationship(back_populates="donation")


class DonationPayment(Base):
    """Map a reference-only many-to-many link between donations and payments."""

    __tablename__ = "donation_payments"

    donation_id: Mapped[int] = mapped_column(ForeignKey("donations.id"), primary_key=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey("payments.id"), primary_key=True)

    donation: Mapped[Donation] = relationship(back_populates="payment_links")
    payment: Mapped["Payment"] = relationship(back_populates="donation_links")
