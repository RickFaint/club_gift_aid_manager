"""SQLAlchemy engine and unit-of-work management.

Tests replace ``db.DB_PATH`` for every case, so the engine is selected lazily
from that current path instead of being frozen when Python imports this module.
Each unit of work owns exactly one SQLAlchemy session and commits or rolls back
all repository changes together.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool


_engines: dict[Path, Engine] = {}
_engine_lock = Lock()


def current_database_path() -> Path:
    """Read the mutable compatibility path used by tests and command tools."""
    from . import db

    return Path(db.DB_PATH).expanduser().resolve()


def get_engine() -> Engine:
    """Return one pooled SQLAlchemy engine for the current SQLite file."""
    path = current_database_path()
    with _engine_lock:
        engine = _engines.get(path)
        if engine is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            # ``db.get_connection`` is request-aware.  During a write request
            # it returns the same deferred-commit DB-API connection used by
            # audit logging, preserving the original all-or-nothing boundary.
            from . import db

            engine = create_engine(
                "sqlite+pysqlite://",
                creator=db.get_connection,
                poolclass=NullPool,
                future=True,
            )

            _engines[path] = engine
        return engine


def new_session() -> Session:
    """Create a session whose objects remain readable after commit."""
    factory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return factory()


@contextmanager
def unit_of_work() -> Iterator[Session]:
    """Commit a successful operation and roll back every exceptional one."""
    session = new_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
