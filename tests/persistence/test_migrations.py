"""Versioned database migration and production revision checks."""

import os
import sqlite3
from types import SimpleNamespace

import pytest
from alembic import command

import club_gift_aid_manager.db as db_module
import club_gift_aid_manager.factory as factory_module
from club_gift_aid_manager.migrations import (
    DatabaseRevisionError,
    alembic_config,
    current_heads,
    expected_heads,
    stamp_database_head,
    upgrade_database,
    verify_database_revision,
)
from club_gift_aid_manager.models import Base


def test_initial_migration_builds_complete_versioned_schema(tmp_path):
    """A fresh database reaches head with tables, seed records, indexes, and triggers."""
    database = tmp_path / "fresh.sqlite3"

    upgrade_database(database)

    assert current_heads(database) == expected_heads(database)
    connection = sqlite3.connect(database)
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        triggers = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        baseline = connection.execute(
            "SELECT car_first_10000_pence FROM mileage_rate_schedules "
            "WHERE effective_from='0001-01-01'"
        ).fetchone()
    finally:
        connection.close()
    assert set(Base.metadata.tables) <= tables
    assert "alembic_version" in tables
    assert triggers == {
        "audit_events_no_update",
        "audit_events_no_delete",
        "privacy_notice_versions_no_update",
        "privacy_notice_versions_no_delete",
    }
    assert baseline == (45,)


def test_production_revision_check_rejects_unversioned_database(tmp_path):
    """Production cannot silently adopt a database with no Alembic revision."""
    database = tmp_path / "unversioned.sqlite3"
    sqlite3.connect(database).execute("CREATE TABLE example (id INTEGER)").connection.close()

    with pytest.raises(DatabaseRevisionError, match="unversioned"):
        verify_database_revision(database)


def test_initial_migration_has_a_working_downgrade(tmp_path):
    """The initial revision can remove its tables, triggers, index, and version row."""
    database = tmp_path / "downgrade.sqlite3"
    upgrade_database(database)

    command.downgrade(alembic_config(database), "base")

    connection = sqlite3.connect(database)
    try:
        objects = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type IN ('table', 'trigger', 'index') AND name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        connection.close()
    assert objects <= {"alembic_version"}


def test_existing_complete_schema_can_be_deliberately_stamped(tmp_path):
    """Operators can adopt a reviewed legacy schema without replaying initial DDL."""
    database = tmp_path / "legacy.sqlite3"
    upgrade_database(database)
    connection = sqlite3.connect(database)
    try:
        connection.execute("DROP TABLE alembic_version")
        connection.commit()
    finally:
        connection.close()

    stamp_database_head(database)

    verify_database_revision(database)


@pytest.mark.parametrize(
    ("ddl", "missing"),
    [
        ("ALTER TABLE users DROP COLUMN must_change_password", "users: must_change_password"),
        ("DROP TRIGGER audit_events_no_update", "audit_events_no_update"),
        ("DROP INDEX uq_payments_request_token", "missing unique constraints"),
    ],
)
def test_current_stamp_does_not_hide_schema_drift(tmp_path, ddl, missing):
    """The revision marker alone cannot disguise a missing runtime requirement."""
    database = tmp_path / "incomplete.sqlite3"
    upgrade_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(ddl)
    connection.close()

    assert current_heads(database) == expected_heads(database)
    with pytest.raises(DatabaseRevisionError, match=missing):
        verify_database_revision(database)


def test_explicit_migration_target_overrides_environment(tmp_path, monkeypatch):
    """Programmatic migrations must operate on the exact caller-selected file."""
    database = tmp_path / "explicit%path.sqlite3"
    other_database = tmp_path / "environment.sqlite3"
    monkeypatch.setenv("CLUB_GIFT_AID_DB", str(other_database))

    upgrade_database(database)

    verify_database_revision(database)
    assert not other_database.exists()


def test_request_transaction_enforces_sqlite_foreign_keys():
    """Every request-owned connection enables FK checks before BEGIN."""
    db_module.begin_request_transaction()
    try:
        connection = db_module.get_connection()
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        db_module.finish_request_transaction(commit=False)


def test_production_factory_verifies_instead_of_upgrading(tmp_path, monkeypatch):
    """Production startup calls the read-only revision guard, never the upgrader."""
    database = tmp_path / "production.sqlite3"
    database.touch()
    monkeypatch.setenv("CLUB_GIFT_AID_ENV", "production")
    monkeypatch.setenv("CLUB_GIFT_AID_SECRET", "x" * 48)
    monkeypatch.setenv("CLUB_GIFT_AID_COOKIE_SECURE", "true")
    monkeypatch.setenv("CLUB_GIFT_AID_TRUSTED_HOSTS", "gift-aid.example.test")
    monkeypatch.setenv("CLUB_GIFT_AID_DB", str(database))
    monkeypatch.setattr(db_module, "DB_PATH", database)
    monkeypatch.setattr(factory_module, "os", SimpleNamespace(name="posix", environ=os.environ))
    monkeypatch.setattr(factory_module, "validate_production_database_path", lambda *_: None)
    monkeypatch.setattr(factory_module, "validate_private_path", lambda path, **_: path)
    verified = []
    monkeypatch.setattr(factory_module, "verify_database_revision", verified.append)
    monkeypatch.setattr(
        factory_module,
        "prepare_development_database",
        lambda _path: pytest.fail("production must not run migrations"),
    )

    factory_module.create_app()

    assert verified == [database]
