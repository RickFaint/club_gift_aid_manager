"""SQLAlchemy repository for mileage rate schedules."""

from ..models import MileageRateSchedule
from .base import SQLAlchemyRepository
from .interfaces import MileageRateRepository


class SQLAlchemyMileageRateRepository(
    SQLAlchemyRepository[MileageRateSchedule, str], MileageRateRepository
):
    """Persist effective-dated mileage schedules keyed by their start date."""

    model_type = MileageRateSchedule
