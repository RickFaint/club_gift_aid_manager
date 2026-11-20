"""Gift Aid declaration model."""

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base
from .types import EncryptedText

if TYPE_CHECKING:
    from .donation import Donation
    from .person import Person
    from .privacy_notice import PrivacyNoticeVersion


class GiftAidDeclaration(Base):
    """Map declaration evidence and the exact privacy notice accepted with it.

    ``Mapped`` annotations describe the Python value while ``mapped_column``
    describes its database representation.  The foreign keys connect a
    declaration to its person and immutable privacy-notice version; matching
    ``relationship`` attributes provide convenient object navigation without
    creating additional database columns.

    The image is base64 because that is the requested portable database format.
    ``EncryptedText`` transparently encrypts it before SQL is issued and decrypts
    it when read.  ``deferred=True`` prevents this potentially large value from
    being selected during ordinary report/list queries; SQLAlchemy loads it only
    when code explicitly accesses ``evidence_image_base64``.
    """

    __tablename__ = "gift_aid_declarations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id"), nullable=False)
    full_name: Mapped[str] = mapped_column(EncryptedText, nullable=False)
    home_address: Mapped[str | None] = mapped_column(EncryptedText)
    postcode: Mapped[str | None] = mapped_column(EncryptedText)
    declaration_date: Mapped[str | None] = mapped_column(Text)
    scope: Mapped[str | None] = mapped_column(EncryptedText)
    withdrawn_date: Mapped[str | None] = mapped_column(Text)
    evidence_ref: Mapped[str | None] = mapped_column(EncryptedText)
    evidence_image_base64: Mapped[str | None] = mapped_column(EncryptedText, deferred=True)
    evidence_image_mime: Mapped[str | None] = mapped_column(Text)
    has_evidence_image: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    privacy_notice_version_id: Mapped[int] = mapped_column(
        ForeignKey("privacy_notice_versions.id"), nullable=False
    )
    privacy_notice_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )

    person: Mapped["Person"] = relationship(back_populates="declarations")
    privacy_notice_version: Mapped["PrivacyNoticeVersion"] = relationship(
        back_populates="declarations"
    )
    donations: Mapped[list["Donation"]] = relationship(back_populates="declaration")
