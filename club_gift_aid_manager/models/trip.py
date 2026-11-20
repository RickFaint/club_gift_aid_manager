"""Mileage trip model and its relationships."""

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base
from .types import EncryptedText

if TYPE_CHECKING:
    from .destination import Destination
    from .payment import PaymentTrip
    from .person import Person


class Trip(Base):
    """Map a mileage claim and the declaration snapshot accepted for that claim."""

    __tablename__ = "trips"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id"), nullable=False)
    origin_id: Mapped[int | None] = mapped_column(ForeignKey("destinations.id"))
    destination_id: Mapped[int | None] = mapped_column(ForeignKey("destinations.id"))
    vehicle_type: Mapped[str] = mapped_column(EncryptedText, nullable=False)
    trip_date: Mapped[str] = mapped_column(Text, nullable=False)
    tax_year: Mapped[str] = mapped_column(Text, nullable=False)
    miles_one_way: Mapped[str] = mapped_column(EncryptedText, nullable=False)
    return_trip: Mapped[int] = mapped_column(Integer, nullable=False)
    total_miles: Mapped[str] = mapped_column(EncryptedText, nullable=False)
    passengers: Mapped[str] = mapped_column(EncryptedText, nullable=False)
    declaration_accepted: Mapped[int] = mapped_column(Integer, nullable=False)
    declaration_text: Mapped[str] = mapped_column(EncryptedText, nullable=False)
    declared_at: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="submitted", server_default=text("'submitted'")
    )
    request_token: Mapped[str | None] = mapped_column(Text, unique=True)

    person: Mapped["Person"] = relationship(back_populates="trips")
    origin: Mapped["Destination | None"] = relationship(
        back_populates="origin_trips", foreign_keys=[origin_id]
    )
    destination: Mapped["Destination | None"] = relationship(
        back_populates="destination_trips", foreign_keys=[destination_id]
    )
    payment_links: Mapped[list["PaymentTrip"]] = relationship(back_populates="trip")


class TripCorrection(Base):
    """Hold an encrypted staff correction alongside the unchanged paid record."""

    __tablename__ = "trip_corrections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trip_id: Mapped[int] = mapped_column(ForeignKey("trips.id"), index=True)
    author_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    note: Mapped[str] = mapped_column(EncryptedText)
    created_at: Mapped[str] = mapped_column(Text, server_default=text("CURRENT_TIMESTAMP"))
