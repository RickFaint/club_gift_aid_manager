"""Alembic environment bound to the application's SQLAlchemy metadata."""

from logging.config import fileConfig
import os
from pathlib import Path

from alembic import context
from sqlalchemy import URL, engine_from_config, pool

from club_gift_aid_manager.models import Base


config = context.config
x_arguments = context.get_x_argument(as_dictionary=True)


def _path_url(value):
    """Build a correctly escaped absolute SQLite URL from an operator-supplied path.

    Using SQLAlchemy's URL constructor avoids platform-specific quoting mistakes,
    particularly for Windows drive letters and paths containing spaces.
    """
    return URL.create("sqlite", database=str(Path(value).expanduser().resolve()))


# Programmatic and ``-x`` paths are deliberate operator choices and must not be
# redirected by a process-wide application environment variable.
database_url = config.attributes.get("database_url")
if not database_url and config.attributes.get("database_path"):
    database_url = _path_url(config.attributes["database_path"])
database_url = database_url or x_arguments.get("database_url")
if not database_url and x_arguments.get("database_path"):
    database_url = _path_url(x_arguments["database_path"])
if not database_url and os.environ.get("CLUB_GIFT_AID_DB"):
    database_url = _path_url(os.environ["CLUB_GIFT_AID_DB"])
database_url = database_url or config.get_main_option("sqlalchemy.url")
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Render SQL without opening a database connection."""
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against the configured database connection."""
    settings = config.get_section(config.config_ini_section, {})
    settings["sqlalchemy.url"] = database_url
    connectable = engine_from_config(
        settings,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
