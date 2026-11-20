"""Reusable SQLAlchemy implementation of the common repository operations."""

from __future__ import annotations

from typing import Generic, Iterable

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from ..models import Base
from .interfaces import KeyT, ModelT, Repository


class SQLAlchemyRepository(Repository[ModelT, KeyT], Generic[ModelT, KeyT]):
    """Implement common repository operations with a caller-owned SQLAlchemy session."""

    model_type: type[ModelT]

    def __init__(self, session: Session) -> None:
        """Bind repository operations to the caller-owned unit-of-work session."""
        self.session = session

    def get(self, identity: KeyT) -> ModelT | None:
        """Load one mapped instance by its SQLAlchemy identity."""
        return self.session.get(self.model_type, identity)

    def query(self) -> Select[tuple[ModelT]]:
        """Build the base select statement subclasses may further constrain."""
        return select(self.model_type)

    def list(self, *, limit: int | None = None, offset: int = 0) -> list[ModelT]:
        """Execute the base query with optional pagination."""
        statement = self.query().offset(offset)
        if limit is not None:
            statement = statement.limit(limit)
        return list(self.session.scalars(statement))

    def count(self) -> int:
        """Count all rows mapped by this concrete repository."""
        return int(self.session.scalar(select(func.count()).select_from(self.model_type)) or 0)

    def add(self, model: ModelT) -> ModelT:
        """Add and flush one model without committing the surrounding session."""
        self.session.add(model)
        self.session.flush()
        return model

    def add_all(self, models: Iterable[ModelT]) -> list[ModelT]:
        """Add and flush several models as part of the same unit of work."""
        result = list(models)
        self.session.add_all(result)
        self.session.flush()
        return result

    def delete(self, model: ModelT) -> None:
        """Delete and flush one model without committing the unit of work."""
        self.session.delete(model)
        self.session.flush()
