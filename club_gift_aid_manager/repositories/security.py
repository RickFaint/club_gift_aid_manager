"""Repositories for audit history and login-throttling state."""

from ..models import AuditEvent, LoginAttempt
from .base import SQLAlchemyRepository
from .interfaces import AuditRepository, LoginAttemptRepository


class SQLAlchemyAuditRepository(SQLAlchemyRepository[AuditEvent, int], AuditRepository):
    """Provide SQLAlchemy persistence for append-only audit metadata."""

    model_type = AuditEvent


class SQLAlchemyLoginAttemptRepository(
    SQLAlchemyRepository[LoginAttempt, str], LoginAttemptRepository
):
    """Persist pseudonymous counters that survive process restarts."""

    model_type = LoginAttempt
