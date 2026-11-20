"""Trip origin/destination model."""

from typing import TYPE_CHECKING

from sqlalchemy import Integer, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base
from .types import EncryptedText

if TYPE_CHECKING:
    from .trip import Trip


class Destination(Base):
    """Map a club or opponent location and its optional geocoding cache."""

    __tablename__ = "destinations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(EncryptedText, nullable=False)
    name_bidx: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    address: Mapped[str | None] = mapped_column(EncryptedText)
    town_city: Mapped[str | None] = mapped_column(EncryptedText)
    postcode: Mapped[str | None] = mapped_column(EncryptedText)
    notes: Mapped[str | None] = mapped_column(EncryptedText)
    active: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    home_ground: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    latitude: Mapped[str | None] = mapped_column(EncryptedText)
    longitude: Mapped[str | None] = mapped_column(EncryptedText)

    origin_trips: Mapped[list["Trip"]] = relationship(
        back_populates="origin", foreign_keys="Trip.origin_id"
    )
    destination_trips: Mapped[list["Trip"]] = relationship(
        back_populates="destination", foreign_keys="Trip.destination_id"
    )
