"""Persistent multi-factor enrollment and verification state."""

from sqlalchemy import ForeignKey, Integer, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .types import EncryptedText


class UserMFA(Base):
    """Store one encrypted TOTP enrollment and its recovery controls per user."""

    __tablename__ = "user_mfa"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    secret: Mapped[str] = mapped_column(EncryptedText, nullable=False)
    recovery_code_hashes: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    last_totp_step: Mapped[int | None] = mapped_column(Integer)
    failed_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    locked_until: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    enabled_at: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
