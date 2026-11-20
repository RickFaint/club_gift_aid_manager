"""SQLAlchemy repository for locations."""

from ..models import Destination
from .base import SQLAlchemyRepository
from .interfaces import DestinationRepository


class SQLAlchemyDestinationRepository(
    SQLAlchemyRepository[Destination, int], DestinationRepository
):
    """Provide SQLAlchemy persistence for saved and geocoded destinations."""

    model_type = Destination
