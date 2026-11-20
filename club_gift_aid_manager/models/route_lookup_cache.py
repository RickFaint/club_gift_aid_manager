"""Shared cache of completed directional route lookups."""

from sqlalchemy import ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .types import EncryptedText


class RouteLookupCache(Base):
    """Persist a route result until either location changes or the entry expires."""

    __tablename__ = "route_lookup_cache"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    origin_id: Mapped[int] = mapped_column(ForeignKey("destinations.id", ondelete="CASCADE"))
    destination_id: Mapped[int] = mapped_column(
        ForeignKey("destinations.id", ondelete="CASCADE")
    )
    origin_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    destination_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    distance_miles: Mapped[str] = mapped_column(EncryptedText, nullable=False)
    expires_at: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
