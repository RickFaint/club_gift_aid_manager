"""Authentication account model."""

from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Integer, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base
from .types import EncryptedText

if TYPE_CHECKING:
    from .person import Person


class User(Base):
    """Map an authenticated account, its role, person link, and session version."""

    __tablename__ = "users"
    __table_args__ = (CheckConstraint("role IN ('admin', 'treasurer', 'volunteer')"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(EncryptedText, nullable=False)
    username_bidx: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    person_id: Mapped[int | None] = mapped_column(ForeignKey("people.id"))
    is_active: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    session_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    must_change_password: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    created_at: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

    person: Mapped["Person | None"] = relationship(back_populates="users")
