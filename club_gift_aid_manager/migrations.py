"""Programmatic Alembic lifecycle used by startup, tests, and operator commands."""

from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import URL, create_engine, inspect


PROJECT_DIRECTORY = Path(__file__).resolve().parent.parent
REQUIRED_TRIGGERS = {
    "audit_events_no_update",
    "audit_events_no_delete",
    "privacy_notice_versions_no_update",
    "privacy_notice_versions_no_delete",
}
REQUIRED_INDEXES = {"idx_destinations_single_home_ground"}
REQUIRED_UNIQUE_COLUMNS = {
    ("users", ("username_bidx",)),
    ("payment_trips", ("trip_id",)),
    ("trips", ("request_token",)),
    ("payments", ("request_token",)),
}
REQUIRED_FOREIGN_KEYS = {
    ("users", ("person_id",), "people", ("id",)),
    ("gift_aid_declarations", ("person_id",), "people", ("id",)),
    ("donations", ("declaration_id",), "gift_aid_declarations", ("id",)),
    ("payment_trips", ("trip_id",), "trips", ("id",)),
    ("payment_trips", ("payment_id",), "payments", ("id",)),
}
REQUIRED_NOT_NULL_COLUMNS = {
    ("users", "must_change_password"),
    ("users", "session_version"),
    ("users", "is_active"),
    ("payment_trips", "payment_id"),
    ("payment_trips", "trip_id"),
}


class DatabaseRevisionError(RuntimeError):
    """Report an absent, unknown, or out-of-date production schema revision."""


def alembic_config(database_path: Path) -> Config:
    """Build an Alembic configuration for one concrete SQLite database path."""
    config = Config(str(PROJECT_DIRECTORY / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_DIRECTORY / "migrations"))
    config.attributes["database_url"] = URL.create(
        "sqlite", database=str(database_path.expanduser().resolve())
    )
    return config


def _read_only_engine(database_path: Path):
    """Inspect an existing SQLite database without creating or modifying it."""
    uri = f"{database_path.expanduser().resolve().as_uri()}?mode=ro"
    return create_engine("sqlite://", creator=lambda: sqlite3.connect(uri, uri=True))


def expected_heads(database_path: Path) -> set[str]:
    """Return the migration heads packaged with this application release."""
    return set(ScriptDirectory.from_config(alembic_config(database_path)).get_heads())


def current_heads(database_path: Path) -> set[str]:
    """Read the revisions recorded in a database without changing its schema."""
    if not database_path.is_file():
        return set()
    engine = _read_only_engine(database_path)
    try:
        with engine.connect() as connection:
            return set(MigrationContext.configure(connection).get_current_heads())
    finally:
        engine.dispose()


def upgrade_database(database_path: Path) -> None:
    """Apply every pending versioned migration to a development or test database."""
    database_path = database_path.expanduser().resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database_path), "head")


def _verify_database_schema(database_path: Path) -> None:
    """Check that current models and important database safeguards exist physically."""
    engine = _read_only_engine(database_path)
    try:
        with engine.connect() as connection:
            inspector = inspect(connection)
            tables = set(inspector.get_table_names())
            columns = {
                table: {column["name"] for column in inspector.get_columns(table)}
                for table in tables
            }
            column_details = {
                (table, column["name"]): column
                for table in tables
                for column in inspector.get_columns(table)
            }
            safeguards = set(
                connection.exec_driver_sql(
                    "SELECT name FROM sqlite_schema WHERE type IN ('trigger', 'index')"
                ).scalars()
            )
            unique_columns = {
                (table, tuple(constraint["column_names"]))
                for table in tables
                for constraint in inspector.get_unique_constraints(table)
            }
            unique_columns |= {
                (table, tuple(index["column_names"]))
                for table in tables
                for index in inspector.get_indexes(table)
                if index.get("unique")
            }
            foreign_keys = {
                (
                    table,
                    tuple(key["constrained_columns"]),
                    key["referred_table"],
                    tuple(key["referred_columns"]),
                )
                for table in tables
                for key in inspector.get_foreign_keys(table)
            }
    finally:
        engine.dispose()
    from .models import Base

    missing = set(Base.metadata.tables) - tables
    if missing:
        raise DatabaseRevisionError(
            "Database schema is incomplete; missing tables: " + ", ".join(sorted(missing))
        )
    incomplete = {
        table: set(model.columns.keys()) - columns.get(table, set())
        for table, model in Base.metadata.tables.items()
        if set(model.columns.keys()) - columns.get(table, set())
    }
    if incomplete:
        details = "; ".join(
            f"{table}: {', '.join(sorted(names))}" for table, names in sorted(incomplete.items())
        )
        raise DatabaseRevisionError("Database schema is incomplete; missing columns: " + details)
    nullable_mismatches = [
        f"{table}.{column} must be NOT NULL"
        for table, column in REQUIRED_NOT_NULL_COLUMNS
        if column_details[(table, column)].get("nullable", True)
        and not column_details[(table, column)].get("primary_key")
    ]
    if nullable_mismatches:
        raise DatabaseRevisionError(
            "Database schema has weakened column constraints: "
            + ", ".join(sorted(nullable_mismatches))
        )
    missing_safeguards = (REQUIRED_TRIGGERS | REQUIRED_INDEXES) - safeguards
    if missing_safeguards:
        raise DatabaseRevisionError(
            "Database schema is incomplete; missing triggers or indexes: "
            + ", ".join(sorted(missing_safeguards))
        )
    missing_unique = REQUIRED_UNIQUE_COLUMNS - unique_columns
    if missing_unique:
        raise DatabaseRevisionError(
            "Database schema is incomplete; missing unique constraints: "
            + ", ".join(
                f"{table}({', '.join(columns)})" for table, columns in sorted(missing_unique)
            )
        )
    missing_foreign_keys = REQUIRED_FOREIGN_KEYS - foreign_keys
    if missing_foreign_keys:
        raise DatabaseRevisionError(
            "Database schema is incomplete; missing foreign keys: "
            + ", ".join(
                f"{table}({', '.join(columns)}) -> {target}({', '.join(target_columns)})"
                for table, columns, target, target_columns in sorted(missing_foreign_keys)
            )
        )


def stamp_database_head(database_path: Path) -> None:
    """Mark a verified existing schema as current without running migration DDL."""
    if not database_path.is_file():
        raise DatabaseRevisionError("Cannot stamp a database that does not exist.")
    _verify_database_schema(database_path)
    command.stamp(alembic_config(database_path), "head")


def verify_database_revision(database_path: Path) -> None:
    """Require the packaged migration heads and their physical schema safeguards."""
    if not database_path.is_file():
        raise DatabaseRevisionError(
            "Production database does not exist; run `alembic upgrade head` before startup."
        )
    current = current_heads(database_path)
    expected = expected_heads(database_path)
    if current != expected:
        current_label = ", ".join(sorted(current)) if current else "unversioned"
        raise DatabaseRevisionError(
            f"Production database revision is {current_label}; expected {', '.join(sorted(expected))}. "
            "Back up the database and run `alembic upgrade head` before startup."
        )
    _verify_database_schema(database_path)


def prepare_development_database(database_path: Path) -> None:
    """Upgrade a versioned database and adopt a complete legacy development schema."""
    if database_path.is_file() and not current_heads(database_path):
        connection = sqlite3.connect(database_path)
        try:
            user_tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
        finally:
            connection.close()
        if user_tables:
            # Compatibility migration upgrades pre-Alembic development databases once;
            # production never enters this path and must be adopted deliberately.
            from .db import create_app_database

            create_app_database()
            stamp_database_head(database_path)
            return
    upgrade_database(database_path)
    verify_database_revision(database_path)
