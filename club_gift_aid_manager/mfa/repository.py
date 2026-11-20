"""Persistence adapter for isolated user MFA enrollment state."""

from ..models import UserMFA
from ..repositories.base import SQLAlchemyRepository


class SQLAlchemyMFARepository(SQLAlchemyRepository[UserMFA, int]):
    """Persist encrypted TOTP state through the common unit-of-work contract."""

    model_type = UserMFA
