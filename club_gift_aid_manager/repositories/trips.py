"""SQLAlchemy repository for mileage trips."""

from ..models import Trip
from .base import SQLAlchemyRepository
from .interfaces import TripRepository


class SQLAlchemyTripRepository(SQLAlchemyRepository[Trip, int], TripRepository):
    """Provide SQLAlchemy persistence for mileage claims."""

    model_type = Trip
