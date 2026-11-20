"""SQLAlchemy repository for people."""

from ..models import Person
from .base import SQLAlchemyRepository
from .interfaces import PersonRepository


class SQLAlchemyPersonRepository(SQLAlchemyRepository[Person, int], PersonRepository):
    """Provide SQLAlchemy persistence for encrypted people records."""

    model_type = Person
