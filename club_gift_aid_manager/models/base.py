"""The single declarative base shared by every SQLAlchemy model."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Supply SQLAlchemy's shared declarative metadata to every ORM model."""

    """Collect table metadata so SQLAlchemy can configure the complete schema."""
