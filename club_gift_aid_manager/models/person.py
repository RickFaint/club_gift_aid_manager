"""Person/contact model and its domain relationships."""

from typing import TYPE_CHECKING

from sqlalchemy import Integer, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base
from .types import EncryptedText

if TYPE_CHECKING:
    from .declaration import GiftAidDeclaration
    from .donation import Donation
    from .payment import Payment
    from .trip import Trip
    from .user import User


class Person(Base):
    """Map a volunteer or staff person and their encrypted contact information."""

    __tablename__ = "people"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(EncryptedText, nullable=False)
    email: Mapped[str | None] = mapped_column(EncryptedText)
    phone: Mapped[str | None] = mapped_column(EncryptedText)
    role: Mapped[str | None] = mapped_column(Text)
    active: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    notes: Mapped[str | None] = mapped_column(EncryptedText)

    users: Mapped[list["User"]] = relationship(back_populates="person")
    trips: Mapped[list["Trip"]] = relationship(back_populates="person")
    declarations: Mapped[list["GiftAidDeclaration"]] = relationship(back_populates="person")
    donations: Mapped[list["Donation"]] = relationship(back_populates="person")
    payments: Mapped[list["Payment"]] = relationship(back_populates="person")
