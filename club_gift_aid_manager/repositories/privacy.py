"""SQLAlchemy repository for immutable privacy notices."""

from sqlalchemy import select

from ..models import PrivacyNoticeVersion
from .base import SQLAlchemyRepository
from .interfaces import PrivacyNoticeRepository


class SQLAlchemyPrivacyNoticeRepository(
    SQLAlchemyRepository[PrivacyNoticeVersion, int], PrivacyNoticeRepository
):
    """Persist immutable privacy notices in publication order."""

    model_type = PrivacyNoticeVersion

    def current(self) -> PrivacyNoticeVersion | None:
        """Load the most recently inserted published privacy notice."""
        return self.session.scalar(
            select(PrivacyNoticeVersion).order_by(PrivacyNoticeVersion.id.desc()).limit(1)
        )
