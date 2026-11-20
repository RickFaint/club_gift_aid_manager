"""SQLAlchemy repository for authentication accounts."""

from sqlalchemy import select

from ..models import User
from .base import SQLAlchemyRepository
from .interfaces import UserRepository


class SQLAlchemyUserRepository(SQLAlchemyRepository[User, int], UserRepository):
    """Persist accounts and perform username lookup without storing plaintext indexes."""

    model_type = User

    def by_username_index(self, username_bidx: str) -> User | None:
        """Return the account matching an HMAC blind index of the username."""
        return self.session.scalar(select(User).where(User.username_bidx == username_bidx))
