"""Append-only audit-event model."""

from sqlalchemy import Integer, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .types import EncryptedText


class AuditEvent(Base):
    """Map append-only security and record-change metadata without changed values."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_user_id: Mapped[int | None] = mapped_column(Integer)
    actor_username: Mapped[str] = mapped_column(EncryptedText, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[str] = mapped_column(Text, nullable=False)
    changed_fields: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default=text("'[]'")
    )
    occurred_at: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
