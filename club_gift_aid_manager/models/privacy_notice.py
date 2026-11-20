"""Immutable privacy-notice version model."""

from typing import TYPE_CHECKING

from sqlalchemy import Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .declaration import GiftAidDeclaration


class PrivacyNoticeVersion(Base):
    """Map one immutable published privacy-notice version and content digest."""

    __tablename__ = "privacy_notice_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    effective_date: Mapped[str] = mapped_column(Text, nullable=False)
    published_date: Mapped[str] = mapped_column(Text, nullable=False)
    text_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    notice_text: Mapped[str] = mapped_column(Text, nullable=False)

    declarations: Mapped[list["GiftAidDeclaration"]] = relationship(
        back_populates="privacy_notice_version"
    )
