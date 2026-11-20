"""Compatibility persistence facade for mileage and Gift Aid operations.

SQLAlchemy models and repositories are the canonical object persistence layer.
This module preserves the established function API while services and routes
move behind repository interfaces. Runtime domain operations use repository
sessions; direct SQLite remains only for connection lifecycle, schema upgrades,
partial indexes, and immutable triggers. Relationship rules remain enforced
below and in services.
"""

from __future__ import annotations

import json
import hashlib
import hmac
import logging
import os
import sqlite3
from contextvars import ContextVar
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from werkzeug.security import generate_password_hash

from .security.crypto import blind_index, decrypt_value, encrypt_value, validate_encryption_config
from .domain.roles import Role
from .services.calculations import (
    BASE_RATE_BANDS,
    calculate_tax_year,
    calculate_trip_amounts,
    rate_schedule_to_bands,
    validate_declaration,
)

DB_PATH = (
    Path(
        os.environ.get(
            "CLUB_GIFT_AID_DB",
            str(Path(__file__).resolve().parent.parent / "club_gift_aid_manager.sqlite3"),
        )
    )
    .expanduser()
    .resolve()
)
INITIAL_PRIVACY_NOTICE_PATH = Path(__file__).resolve().parent.parent / "privacy-notice.txt"

_WINDOWS_PERMISSION_WARNING_EMITTED = False

ENCRYPTED_FIELDS: dict[str, tuple[str, ...]] = {
    "people": ("name", "email", "phone", "notes"),
    "users": ("username",),
    "gift_aid_declarations": ("full_name", "home_address", "postcode", "scope", "evidence_ref"),
    "destinations": ("name", "address", "town_city", "postcode", "notes", "latitude", "longitude"),
    "trips": ("vehicle_type", "miles_one_way", "total_miles", "passengers", "declaration_text"),
    "donations": ("amount_pence", "method", "reference", "bank_reference", "receipt_id"),
    "payments": ("batch_id", "amount_pence", "method", "reference"),
    "audit_events": ("actor_username",),
    "route_lookup_cache": ("distance_miles",),
}

FIELD_CASTS = {
    ("destinations", "latitude"): float,
    ("destinations", "longitude"): float,
    ("trips", "miles_one_way"): float,
    ("trips", "total_miles"): float,
    ("trips", "passengers"): int,
    ("donations", "amount_pence"): int,
    ("donations", "receipt_id"): int,
    ("payments", "batch_id"): int,
    ("payments", "amount_pence"): int,
}

_REQUEST_CONNECTION: ContextVar[sqlite3.Connection | None] = ContextVar(
    "club_gift_aid_request_connection",
    default=None,
)


class _RequestConnection(sqlite3.Connection):
    """Keep helper-level commits and closes inside the surrounding request transaction."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Create a connection whose final commit is controlled by Flask hooks."""
        super().__init__(*args, **kwargs)
        self.defer_commit = False
        self.defer_close = False
        self.defer_rollback = False

    def commit(self) -> None:
        """Defer normal commits until the request lifecycle declares success."""
        if not self.defer_commit:
            super().commit()

    def close(self) -> None:
        """Keep the request-owned connection open until teardown completes."""
        if not self.defer_close:
            super().close()

    def rollback(self) -> None:
        """Let Flask, rather than an inner ORM session, own request rollback."""
        if not self.defer_rollback:
            super().rollback()


def get_connection() -> sqlite3.Connection:
    """Open the configured SQLite database and return rows by column name."""
    request_connection = _REQUEST_CONNECTION.get()
    if request_connection is not None:
        return request_connection

    global _WINDOWS_PERMISSION_WARNING_EMITTED
    DB_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        os.chmod(DB_PATH.parent, 0o700)
        descriptor = os.open(DB_PATH, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
    elif not _WINDOWS_PERMISSION_WARNING_EMITTED:
        logging.getLogger(__name__).warning(
            "Windows development mode cannot enforce POSIX database permissions; "
            "do not use this configuration for production data."
        )
        _WINDOWS_PERMISSION_WARNING_EMITTED = True
    conn = sqlite3.connect(DB_PATH, factory=_RequestConnection)
    conn.row_factory = sqlite3.Row
    # SQLite ignores changes to this setting once a transaction has started.
    conn.execute("PRAGMA foreign_keys = ON")
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        conn.close()
        raise RuntimeError("SQLite foreign-key enforcement could not be enabled.")
    return conn


def begin_request_transaction() -> None:
    """Share one deferred-commit connection across all database helpers in a write request."""
    if _REQUEST_CONNECTION.get() is not None:
        raise RuntimeError("A request database transaction is already active.")
    conn = get_connection()
    if not isinstance(conn, _RequestConnection):
        conn.close()
        raise RuntimeError("Request transactions require the managed SQLite connection.")
    try:
        conn.execute("BEGIN")
    except Exception:
        conn.close()
        raise
    conn.defer_commit = True
    conn.defer_close = True
    conn.defer_rollback = True
    _REQUEST_CONNECTION.set(conn)


def finish_request_transaction(*, commit: bool) -> None:
    """Commit a successful request or roll back its changes and audit events together."""
    conn = _REQUEST_CONNECTION.get()
    if conn is None:
        return
    _REQUEST_CONNECTION.set(None)
    conn.defer_commit = False
    conn.defer_close = False
    conn.defer_rollback = False
    try:
        if commit:
            sqlite3.Connection.commit(conn)
        else:
            sqlite3.Connection.rollback(conn)
    finally:
        sqlite3.Connection.close(conn)


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """Convert an optional SQLite row into a JSON-friendly mapping."""
    return dict(row) if row is not None else None


def _encrypt_fields(table: str, values: dict[str, Any]) -> dict[str, Any]:
    """Copy values and encrypt the sensitive fields belonging to a table."""
    result = dict(values)
    for field in ENCRYPTED_FIELDS[table]:
        if field in result:
            result[field] = encrypt_value(result[field])
    return result


def _decrypt_fields(table: str, row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
    """Convert and decrypt one database record, preserving public return types."""
    if row is None:
        return None
    result = dict(row)
    for field in ENCRYPTED_FIELDS[table]:
        if field in result:
            value = decrypt_value(result[field])
            caster = FIELD_CASTS.get((table, field))
            result[field] = caster(value) if value is not None and caster else value
    return result


def _decrypt_alias(row: dict[str, Any], field: str, caster=None) -> None:
    """Decrypt an aliased query field in place and optionally coerce its type."""
    if field not in row:
        return
    value = decrypt_value(row[field])
    row[field] = caster(value) if value is not None and caster else value


def create_app_database() -> None:
    """Create missing tables and migrate columns for databases from older app versions.

    The schema is kept in one idempotent script. The following column checks
    add newer fields only when absent, preserving existing stored records.
    """
    validate_encryption_config()
    # SQLAlchemy models are the canonical table definitions.  The idempotent
    # SQL below remains for data migrations, partial indexes, and SQLite
    # immutability triggers that are intentionally database-specific.
    from .database import get_engine
    from .models import Base

    Base.metadata.create_all(get_engine())
    conn = get_connection()
    try:
        conn.executescript(
            """
            -- Club contacts that may own claims, donations, declarations, or payments.
            CREATE TABLE IF NOT EXISTS people (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT,
                phone TEXT,
                role TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                notes TEXT
            );

            -- Login identities are separate from people and may optionally link to one.
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                username_bidx TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin', 'treasurer', 'volunteer')),
                person_id INTEGER,
                is_active INTEGER NOT NULL DEFAULT 1,
                session_version INTEGER NOT NULL DEFAULT 1,
                must_change_password INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(person_id) REFERENCES people(id)
            );

            -- Published privacy notices are append-only evidence of what users were shown.
            CREATE TABLE IF NOT EXISTS privacy_notice_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                effective_date TEXT NOT NULL,
                published_date TEXT NOT NULL,
                text_sha256 TEXT NOT NULL,
                notice_text TEXT NOT NULL
            );

            -- Donor declaration evidence; donations link to the declaration record.
            CREATE TABLE IF NOT EXISTS gift_aid_declarations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                person_id INTEGER NOT NULL,
                full_name TEXT NOT NULL,
                home_address TEXT,
                postcode TEXT,
                declaration_date TEXT,
                scope TEXT,
                withdrawn_date TEXT,
                evidence_ref TEXT,
                evidence_image_base64 TEXT,
                evidence_image_mime TEXT,
                has_evidence_image INTEGER NOT NULL DEFAULT 0,
                privacy_notice_version_id INTEGER NOT NULL,
                privacy_notice_sha256 TEXT NOT NULL,
                FOREIGN KEY(person_id) REFERENCES people(id),
                FOREIGN KEY(privacy_notice_version_id) REFERENCES privacy_notice_versions(id)
            );

            -- Geocodable grounds used as trip origins and destinations.
            CREATE TABLE IF NOT EXISTS destinations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                name_bidx TEXT NOT NULL UNIQUE,
                address TEXT,
                town_city TEXT,
                postcode TEXT,
                notes TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                home_ground INTEGER NOT NULL DEFAULT 0,
                latitude TEXT,
                longitude TEXT
            );

            -- Complete mileage rate schedules. Period end dates are derived from
            -- the next effective date, so historical schedules remain intact.
            CREATE TABLE IF NOT EXISTS mileage_rate_schedules (
                effective_from TEXT PRIMARY KEY,
                car_first_10000_pence INTEGER NOT NULL CHECK(car_first_10000_pence >= 0),
                car_over_10000_pence INTEGER NOT NULL CHECK(car_over_10000_pence >= 0),
                motorcycle_pence INTEGER NOT NULL CHECK(motorcycle_pence >= 0),
                bicycle_pence INTEGER NOT NULL CHECK(bicycle_pence >= 0),
                passenger_pence INTEGER NOT NULL CHECK(passenger_pence >= 0),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            -- Submitted mileage claims retain the declaration wording shown at submission.
            CREATE TABLE IF NOT EXISTS trips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                person_id INTEGER NOT NULL,
                origin_id INTEGER,
                destination_id INTEGER,
                vehicle_type TEXT NOT NULL,
                trip_date TEXT NOT NULL,
                tax_year TEXT NOT NULL,
                miles_one_way TEXT NOT NULL,
                return_trip INTEGER NOT NULL,
                total_miles TEXT NOT NULL,
                passengers TEXT NOT NULL,
                declaration_accepted INTEGER NOT NULL,
                declaration_text TEXT NOT NULL,
                declared_at TEXT,
                status TEXT NOT NULL DEFAULT 'submitted',
                FOREIGN KEY(person_id) REFERENCES people(id),
                FOREIGN KEY(origin_id) REFERENCES destinations(id),
                FOREIGN KEY(destination_id) REFERENCES destinations(id)
            );

            -- Donations are independent from reimbursement payments; relationship rows
            -- are references only and do not allocate or offset donation amounts.
            CREATE TABLE IF NOT EXISTS donations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                person_id INTEGER NOT NULL,
                amount_pence TEXT NOT NULL,
                donated_date TEXT,
                method TEXT,
                reference TEXT,
                declaration_id INTEGER,
                gift_aid_eligible INTEGER NOT NULL DEFAULT 0,
                gift_aid_claimed_date TEXT,
                linked_payment_id INTEGER,
                received_date TEXT,
                bank_reference TEXT,
                receipt_id TEXT,
                FOREIGN KEY(person_id) REFERENCES people(id),
                FOREIGN KEY(declaration_id) REFERENCES gift_aid_declarations(id)
            );

            -- Money paid to a person as reimbursement for one or more complete trips.
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                person_id INTEGER NOT NULL,
                batch_id TEXT,
                amount_pence TEXT NOT NULL,
                paid_date TEXT,
                method TEXT,
                reference TEXT,
                FOREIGN KEY(person_id) REFERENCES people(id)
            );

            CREATE TABLE IF NOT EXISTS donation_payments (
                donation_id INTEGER NOT NULL,
                payment_id INTEGER NOT NULL,
                PRIMARY KEY(donation_id, payment_id),
                FOREIGN KEY(donation_id) REFERENCES donations(id),
                FOREIGN KEY(payment_id) REFERENCES payments(id)
            );

            -- Join table allows one payment to cover many trips; UNIQUE(trip_id)
            -- ensures a trip cannot be reimbursed by more than one payment.
            CREATE TABLE IF NOT EXISTS payment_trips (
                payment_id INTEGER NOT NULL,
                trip_id INTEGER NOT NULL UNIQUE,
                PRIMARY KEY(payment_id, trip_id),
                FOREIGN KEY(payment_id) REFERENCES payments(id),
                FOREIGN KEY(trip_id) REFERENCES trips(id)
            );

            -- Append-only metadata history; changed values and passwords are not stored.
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_user_id INTEGER,
                actor_username TEXT NOT NULL,
                action TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                changed_fields TEXT NOT NULL DEFAULT '[]',
                occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            -- Audit history is append-only.  Even application defects cannot
            -- silently rewrite or remove an event once it has been recorded.
            CREATE TRIGGER IF NOT EXISTS audit_events_no_update
            BEFORE UPDATE ON audit_events
            BEGIN
                SELECT RAISE(ABORT, 'audit events are immutable');
            END;

            CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
            BEFORE DELETE ON audit_events
            BEGIN
                SELECT RAISE(ABORT, 'audit events are immutable');
            END;

            -- Short-lived, pseudonymous counters throttle repeated login guesses.
            CREATE TABLE IF NOT EXISTS login_attempts (
                attempt_key TEXT PRIMARY KEY,
                failure_count INTEGER NOT NULL,
                window_started_at INTEGER NOT NULL,
                locked_until INTEGER NOT NULL DEFAULT 0
            );

            CREATE TRIGGER IF NOT EXISTS privacy_notice_versions_no_update
            BEFORE UPDATE ON privacy_notice_versions
            BEGIN
                SELECT RAISE(ABORT, 'Published privacy notice versions are immutable.');
            END;

            CREATE TRIGGER IF NOT EXISTS privacy_notice_versions_no_delete
            BEFORE DELETE ON privacy_notice_versions
            BEGIN
                SELECT RAISE(ABORT, 'Published privacy notice versions are immutable.');
            END;
            """
        )
        if conn.execute("SELECT 1 FROM privacy_notice_versions LIMIT 1").fetchone() is None:
            try:
                initial_notice = INITIAL_PRIVACY_NOTICE_PATH.read_text(encoding="utf-8")
            except FileNotFoundError:
                initial_notice = (
                    "The club privacy statement has not been configured. "
                    "Please contact the club administrator."
                )
            conn.execute(
                """INSERT INTO privacy_notice_versions
                       (effective_date, published_date, text_sha256, notice_text)
                   VALUES (?, ?, ?, ?)""",
                (
                    date.today().isoformat(),
                    date.today().isoformat(),
                    hashlib.sha256(initial_notice.encode("utf-8")).hexdigest(),
                    initial_notice,
                ),
            )
        # Preserve the app's former rates for all dates before the first admin change.
        conn.execute(
            """INSERT OR IGNORE INTO mileage_rate_schedules (
                   effective_from, car_first_10000_pence, car_over_10000_pence,
                   motorcycle_pence, bicycle_pence, passenger_pence
               ) VALUES ('0001-01-01', ?, ?, ?, ?, ?)""",
            (
                int(BASE_RATE_BANDS["car"]["first_10000"] * 100),
                int(BASE_RATE_BANDS["car"]["over_10000"] * 100),
                int(BASE_RATE_BANDS["motorcycle"]["first_10000"] * 100),
                int(BASE_RATE_BANDS["bicycle"]["first_10000"] * 100),
                int(BASE_RATE_BANDS["passenger"] * 100),
            ),
        )
        conn.commit()
        # Inspect the existing schema before adding fields so upgrades are repeatable.
        trip_columns = {row["name"] for row in conn.execute("PRAGMA table_info(trips)").fetchall()}
        destination_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(destinations)").fetchall()
        }
        user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        # Existing accounts begin at version one.  Changing this value later
        # makes sessions issued with an older version immediately invalid.
        if "session_version" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 1")
        # Legacy databases predate administrator-assisted password resets. Existing
        # accounts keep their current passwords until an administrator resets one.
        if "must_change_password" not in user_columns:
            conn.execute(
                "ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0"
            )
        payment_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(payments)").fetchall()
        }
        if "calculation_snapshot" not in payment_columns:
            conn.execute("ALTER TABLE payments ADD COLUMN calculation_snapshot TEXT")
        # New location flags default off so no legacy location is assumed to be home.
        if "home_ground" not in destination_columns:
            conn.execute(
                "ALTER TABLE destinations ADD COLUMN home_ground INTEGER NOT NULL DEFAULT 0"
            )
        # Coordinates are a cache; a missing cache triggers geocoding on first use.
        if "latitude" not in destination_columns:
            conn.execute("ALTER TABLE destinations ADD COLUMN latitude REAL")
        if "longitude" not in destination_columns:
            conn.execute("ALTER TABLE destinations ADD COLUMN longitude REAL")
        donation_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(donations)").fetchall()
        }
        declaration_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(gift_aid_declarations)").fetchall()
        }
        # Existing gifts remain included in totals when the dormancy feature is introduced.
        if "active" not in donation_columns:
            conn.execute("ALTER TABLE donations ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        if "active" not in declaration_columns:
            conn.execute(
                "ALTER TABLE gift_aid_declarations ADD COLUMN active INTEGER NOT NULL DEFAULT 1"
            )
        if "evidence_image_base64" not in declaration_columns:
            conn.execute("ALTER TABLE gift_aid_declarations ADD COLUMN evidence_image_base64 TEXT")
        if "evidence_image_mime" not in declaration_columns:
            conn.execute("ALTER TABLE gift_aid_declarations ADD COLUMN evidence_image_mime TEXT")
        if "has_evidence_image" not in declaration_columns:
            conn.execute(
                "ALTER TABLE gift_aid_declarations "
                "ADD COLUMN has_evidence_image INTEGER NOT NULL DEFAULT 0"
            )
            conn.execute(
                "UPDATE gift_aid_declarations SET has_evidence_image = 1 "
                "WHERE evidence_image_base64 IS NOT NULL AND evidence_image_base64 <> ''"
            )
        current_notice = conn.execute(
            "SELECT id, text_sha256 FROM privacy_notice_versions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if "privacy_notice_version_id" not in declaration_columns:
            conn.execute(
                "ALTER TABLE gift_aid_declarations ADD COLUMN privacy_notice_version_id INTEGER"
            )
            conn.execute(
                "UPDATE gift_aid_declarations SET privacy_notice_version_id = ?",
                (current_notice["id"],),
            )
        if "privacy_notice_sha256" not in declaration_columns:
            conn.execute("ALTER TABLE gift_aid_declarations ADD COLUMN privacy_notice_sha256 TEXT")
            conn.execute(
                "UPDATE gift_aid_declarations SET privacy_notice_sha256 = ?",
                (current_notice["text_sha256"],),
            )
        # Preserve existing single-payment links in the new many-to-many relation.
        conn.execute(
            """INSERT OR IGNORE INTO donation_payments (donation_id, payment_id)
               SELECT id, linked_payment_id FROM donations
               WHERE linked_payment_id IS NOT NULL"""
        )
        # Legacy trip records are preserved and receive nullable location links.
        if "destination_id" not in trip_columns:
            conn.execute(
                "ALTER TABLE trips ADD COLUMN destination_id INTEGER REFERENCES destinations(id)"
            )
        if "origin_id" not in trip_columns:
            conn.execute(
                "ALTER TABLE trips ADD COLUMN origin_id INTEGER REFERENCES destinations(id)"
            )
        # The partial index permits unflagged rows while preventing two home flags.
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_destinations_single_home_ground
               ON destinations(home_ground) WHERE home_ground = 1"""
        )
        conn.commit()
    finally:
        conn.close()


def record_audit_event(
    *,
    actor_user_id: int | None,
    actor_username: str,
    action: str,
    entity_type: str,
    entity_id: int | str,
    changed_fields: Iterable[str] = (),
) -> None:
    """Append a user/action/entity event without storing sensitive field values."""
    from .database import unit_of_work
    from .models import AuditEvent
    from .repositories import SQLAlchemyAuditRepository

    with unit_of_work() as session:
        SQLAlchemyAuditRepository(session).add(
            AuditEvent(
                actor_user_id=actor_user_id,
                actor_username=actor_username,
                action=action,
                entity_type=entity_type,
                entity_id=str(entity_id),
                changed_fields=json.dumps(sorted(set(changed_fields))),
            )
        )


def record_audit_events(
    *,
    actor_user_id: int | None,
    actor_username: str,
    events: Iterable[dict[str, Any]],
) -> None:
    """Append many audit events through one database unit of work.

    CSV imports can create hundreds or thousands of records. Opening a new
    SQLAlchemy session for every audit row is unnecessarily expensive and is
    especially fragile with SQLite because all those sessions share the import's
    outer atomic transaction. This bulk form preserves an individual audit row
    for every imported entity while flushing them through one session.
    """
    from .database import unit_of_work
    from .models import AuditEvent
    from .repositories import SQLAlchemyAuditRepository

    audit_rows = [
        AuditEvent(
            actor_user_id=actor_user_id,
            actor_username=actor_username,
            action=event["action"],
            entity_type=event["entity_type"],
            entity_id=str(event["entity_id"]),
            changed_fields=json.dumps(sorted(set(event.get("changed_fields", ())))),
        )
        for event in events
    ]
    if not audit_rows:
        return
    with unit_of_work() as session:
        SQLAlchemyAuditRepository(session).add_all(audit_rows)


def get_login_lock_seconds(
    attempt_keys: Iterable[str],
    *,
    now: int,
    window_seconds: int = 15 * 60,
) -> int:
    """Return active lockouts and remove expired transient counters."""
    keys = tuple(set(attempt_keys))
    if not keys:
        return 0
    from sqlalchemy import delete, func, select
    from .database import unit_of_work
    from .models import LoginAttempt

    with unit_of_work() as session:
        session.execute(
            delete(LoginAttempt).where(
                LoginAttempt.locked_until <= now,
                LoginAttempt.window_started_at < now - window_seconds,
            )
        )
        locked_until = session.scalar(
            select(func.max(LoginAttempt.locked_until)).where(
                LoginAttempt.attempt_key.in_(keys),
            )
        )
        return max(0, int(locked_until or 0) - now)


def record_login_failure(
    attempt_keys: Iterable[str],
    *,
    now: int,
    window_seconds: int,
    max_failures: int | dict[str, int],
    lockout_seconds: int,
) -> tuple[bool, int]:
    """Increment login counters and report whether this failure began a lockout."""
    keys = tuple(set(attempt_keys))
    if not keys:
        raise ValueError("At least one login attempt key is required.")
    newly_locked = False
    longest_lock = 0
    from sqlalchemy import delete
    from .database import unit_of_work
    from .models import LoginAttempt
    from .repositories import SQLAlchemyLoginAttemptRepository

    with unit_of_work() as session:
        repository = SQLAlchemyLoginAttemptRepository(session)
        session.execute(
            delete(LoginAttempt).where(
                LoginAttempt.window_started_at < now - window_seconds - lockout_seconds,
                LoginAttempt.locked_until < now,
            )
        )
        for key in keys:
            row = repository.get(key)
            if row is None or now - row.window_started_at >= window_seconds:
                failure_count = 1
                window_started_at = now
            else:
                failure_count = row.failure_count + 1
                window_started_at = row.window_started_at
            threshold = max_failures[key] if isinstance(max_failures, dict) else max_failures
            locked_until = now + lockout_seconds if failure_count >= threshold else 0
            newly_locked = newly_locked or (
                locked_until > now and (row is None or row.locked_until <= now)
            )
            if row is None:
                repository.add(
                    LoginAttempt(
                        attempt_key=key,
                        failure_count=failure_count,
                        window_started_at=window_started_at,
                        locked_until=locked_until,
                    )
                )
            else:
                row.failure_count = failure_count
                row.window_started_at = window_started_at
                row.locked_until = locked_until
            longest_lock = max(longest_lock, locked_until - now)
        return newly_locked, max(0, longest_lock)


def clear_login_failures(attempt_keys: Iterable[str]) -> None:
    """Clear the successful user's and source address's transient failure counters."""
    keys = tuple(set(attempt_keys))
    if not keys:
        return
    from sqlalchemy import delete
    from .database import unit_of_work
    from .models import LoginAttempt

    with unit_of_work() as session:
        session.execute(delete(LoginAttempt).where(LoginAttempt.attempt_key.in_(keys)))


def list_audit_events(
    limit: int = 500, offset: int = 0, *, with_total: bool = False, search: str = ""
) -> list[dict[str, Any]] | tuple[list[dict[str, Any]], int]:
    """Return newest audit events, bounded to prevent an unbounded page response."""
    from sqlalchemy import func, select
    from .database import unit_of_work
    from .dtos import AuditEventDTO
    from .models import AuditEvent

    with unit_of_work() as session:
        total = int(session.scalar(select(func.count()).select_from(AuditEvent)) or 0)
        statement = select(AuditEvent).order_by(AuditEvent.id.desc())
        if not search:
            statement = statement.limit(max(1, min(int(limit), 1000))).offset(max(0, int(offset)))
        models = session.scalars(statement)
        events = [AuditEventDTO.model_validate(model).model_dump() for model in models]
        if search:
            query = search.casefold()
            events = [
                event for event in events if query in json.dumps(event, default=str).casefold()
            ]
            total = len(events)
            events = events[offset : offset + max(1, min(int(limit), 1000))]
        return (events, total) if with_total else events


def _user_model_to_dict(user, *, include_credentials: bool = False) -> dict[str, Any]:
    """Serialize a User model through the safe response DTO."""
    from .dtos import UserDTO

    result = UserDTO.model_validate(user).model_dump()
    result["is_active"] = int(result["is_active"])
    if include_credentials:
        # Only the authentication service requests these internal fields.  They
        # are absent from UserDTO and therefore cannot leak from normal routes.
        result["username_bidx"] = user.username_bidx
        result["password_hash"] = user.password_hash
    return result


def create_user(
    *,
    username: str,
    password: str,
    role: str,
    person_id: int | None = None,
    must_change_password: bool = False,
) -> dict[str, Any]:
    """Validate a login identity, hash its password, and persist the account."""
    if not username.strip():
        raise ValueError("Username is required.")
    if role not in {member.value for member in Role}:
        raise ValueError("Role must be admin, treasurer, or volunteer.")
    from .security.passwords import validate_password

    validate_password(password)
    if role == Role.VOLUNTEER.value and person_id is None:
        raise ValueError("Volunteer accounts must be linked to a person.")
    if role == Role.ADMIN.value and person_id is not None:
        raise ValueError("Administrator accounts cannot be linked to a person.")
    from .database import unit_of_work
    from .models import User
    from .repositories import SQLAlchemyPersonRepository, SQLAlchemyUserRepository

    with unit_of_work() as session:
        if person_id is not None and SQLAlchemyPersonRepository(session).get(person_id) is None:
            raise ValueError("Linked person was not found.")
        user = SQLAlchemyUserRepository(session).add(
            User(
                username=username.strip(),
                username_bidx=blind_index(username),
                password_hash=generate_password_hash(password),
                role=role,
                person_id=person_id,
                is_active=1,
                session_version=1,
                must_change_password=int(must_change_password),
            )
        )
        return _user_model_to_dict(user)


def validate_user_import(
    rows: list[tuple[int, dict[str, Any]]],
) -> list[str]:
    """Check all account rows and resolve referenced records in batches."""
    from sqlalchemy import select

    from .database import unit_of_work
    from .models import Person, User
    from .security.passwords import validate_password

    indexes = {blind_index(values["username"]) for _, values in rows}
    person_ids = {values["person_id"] for _, values in rows if values["person_id"] is not None}
    with unit_of_work() as session:
        existing_indexes = set(
            session.scalars(select(User.username_bidx).where(User.username_bidx.in_(indexes)))
        )
        existing_people = set(session.scalars(select(Person.id).where(Person.id.in_(person_ids))))

    errors = []
    seen_indexes = set()
    for number, values in rows:
        username = values["username"]
        role = values["role"]
        person_id = values["person_id"]
        username_bidx = blind_index(username)
        try:
            if values["password"] is not None:
                validate_password(values["password"])
            if role == Role.VOLUNTEER.value and person_id is None:
                raise ValueError("Volunteer accounts must be linked to a person.")
            if role == Role.ADMIN.value and person_id is not None:
                raise ValueError("Administrator accounts cannot be linked to a person.")
            if person_id is not None and person_id not in existing_people:
                raise ValueError(f"Linked person {person_id} was not found.")
            if username_bidx in seen_indexes:
                raise ValueError("Username appears more than once in the CSV.")
            if username_bidx in existing_indexes:
                raise ValueError("Username is already in use.")
        except ValueError as exc:
            errors.append(f"Row {number}: {exc}")
        seen_indexes.add(username_bidx)
    return errors


def prepare_user_import(users: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Hash validated passwords before opening the import write transaction."""
    from secrets import token_urlsafe

    return [
        {
            "username": values["username"],
            "username_bidx": blind_index(values["username"]),
            "password_hash": generate_password_hash(values["password"] or token_urlsafe(48)),
            "role": values["role"],
            "person_id": values["person_id"],
            "must_change_password": 1,
        }
        for values in users
    ]


def create_users(users: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Persist prepared accounts, with uniqueness and person links enforced by SQLite."""
    from .database import unit_of_work
    from .models import User
    from .repositories import SQLAlchemyUserRepository

    with unit_of_work() as session:
        created = SQLAlchemyUserRepository(session).add_all(User(**values) for values in users)
        return [_user_model_to_dict(model) for model in created]


def get_user_by_username(username: str) -> dict[str, Any] | None:
    """Find an account case-insensitively; includes its hash for login checking."""
    from .database import unit_of_work
    from .repositories import SQLAlchemyUserRepository

    with unit_of_work() as session:
        user = SQLAlchemyUserRepository(session).by_username_index(blind_index(username))
        return _user_model_to_dict(user, include_credentials=True) if user else None


def get_user_by_id(user_id: int) -> dict[str, Any] | None:
    """Load a session account without exposing its password hash."""
    from .database import unit_of_work
    from .repositories import SQLAlchemyUserRepository

    with unit_of_work() as session:
        user = SQLAlchemyUserRepository(session).get(user_id)
        return _user_model_to_dict(user) if user else None


def reset_user_password(user_id: int, temporary_password: str) -> dict[str, Any] | None:
    """Set a temporary password, require replacement, and invalidate all sessions."""
    from .security.passwords import validate_password

    validate_password(temporary_password, label="Temporary password")
    from .database import unit_of_work
    from .repositories import SQLAlchemyUserRepository

    with unit_of_work() as session:
        user = SQLAlchemyUserRepository(session).get(user_id)
        if user is None:
            return None
        user.password_hash = generate_password_hash(temporary_password)
        user.must_change_password = 1
        user.session_version += 1
        return _user_model_to_dict(user)


def replace_temporary_password(user_id: int, new_password: str) -> dict[str, Any] | None:
    """Replace a temporary password and clear the mandatory-change state."""
    from werkzeug.security import check_password_hash
    from .security.passwords import validate_password

    validate_password(new_password)
    from .database import unit_of_work
    from .repositories import SQLAlchemyUserRepository

    with unit_of_work() as session:
        user = SQLAlchemyUserRepository(session).get(user_id)
        if user is None:
            return None
        if not user.must_change_password:
            raise ValueError("This account does not require a password change.")
        if check_password_hash(user.password_hash, new_password):
            raise ValueError("Choose a password different from the temporary password.")
        user.password_hash = generate_password_hash(new_password)
        user.must_change_password = 0
        user.session_version += 1
        return _user_model_to_dict(user)


def _matches_list_search(query: str, *values: Any) -> bool:
    """Check case-insensitive search without storing plaintext indexes."""
    return any(query in str(value).casefold() for value in values if value is not None)


def list_users(
    *, limit: int | None = None, offset: int = 0, with_total: bool = False, search: str = ""
) -> list[dict[str, Any]] | tuple[list[dict[str, Any]], int]:
    """List account metadata for the admin account-management view."""
    from sqlalchemy import func, select

    from .database import unit_of_work
    from .models import Person, User, UserMFA

    with unit_of_work() as session:
        statement = select(
            User.id,
            User.username,
            User.role,
            User.person_id,
            User.is_active,
            User.must_change_password,
            UserMFA.enabled_at,
        ).outerjoin(UserMFA, UserMFA.user_id == User.id)

        def serialize(row) -> dict[str, Any]:
            """Convert one projected account row into the stable public API shape.

            Keeping this conversion beside the deliberately narrow query makes it
            clear which authentication fields may leave the persistence layer.
            """
            return {
                "id": row.id,
                "username": row.username,
                "role": row.role,
                "person_id": row.person_id,
                "is_active": int(bool(row.is_active)),
                "must_change_password": bool(row.must_change_password),
                "mfa_enabled": bool(row.enabled_at),
            }

        query = search.casefold()
        if query:
            searchable = session.execute(
                select(User.id, User.username, User.role, User.is_active, Person.name).outerjoin(
                    Person, User.person_id == Person.id
                )
            )
            names = [
                (user_id, name)
                for user_id, name, role, active, person_name in searchable
                if _matches_list_search(
                    query, name, role, "active" if active else "inactive", person_name
                )
            ]
            ids = [
                record[0]
                for record in sorted(names, key=lambda item: (item[1].casefold(), item[0]))
            ]
            total = len(ids)
            if limit is not None:
                ids = ids[offset : offset + limit]
            if not ids:
                return ([], total) if with_total else []
            records = {
                row.id: serialize(row) for row in session.execute(statement.where(User.id.in_(ids)))
            }
            users = [records[user_id] for user_id in ids if user_id in records]
        else:
            total = (
                int(session.scalar(select(func.count()).select_from(User)) or 0)
                if with_total
                else 0
            )
            statement = statement.order_by(User.id)
            if limit is not None:
                statement = statement.limit(limit).offset(offset)
            users = [serialize(row) for row in session.execute(statement)]
        return (users, total) if with_total else users


_UNSET_PERSON_LINK = object()


def update_user(
    user_id: int,
    *,
    role: str | None = None,
    person_id: int | None | object = _UNSET_PERSON_LINK,
    is_active: bool | None = None,
) -> dict[str, Any] | None:
    """Change role/link/activation while protecting the last active administrator."""
    from sqlalchemy import func, select
    from .database import unit_of_work
    from .models import User
    from .repositories import SQLAlchemyPersonRepository, SQLAlchemyUserRepository

    with unit_of_work() as session:
        user = SQLAlchemyUserRepository(session).get(user_id)
        if user is None:
            return None
        next_role = role or user.role
        next_person_id = user.person_id if person_id is _UNSET_PERSON_LINK else person_id
        if next_role == Role.ADMIN.value:
            next_person_id = None
        if next_role not in {member.value for member in Role}:
            raise ValueError("Role must be admin, treasurer, or volunteer.")
        if next_role == Role.VOLUNTEER.value and next_person_id is None:
            raise ValueError("Cannot assign volunteer role without a linked person.")
        if (
            next_person_id is not None
            and SQLAlchemyPersonRepository(session).get(next_person_id) is None
        ):
            raise ValueError("Linked person was not found.")
        if (
            user.is_active
            and user.role == Role.ADMIN.value
            and (next_role != Role.ADMIN.value or is_active is False)
        ):
            active_admins = session.scalar(
                select(func.count())
                .select_from(User)
                .where(
                    User.role == Role.ADMIN.value,
                    User.is_active == 1,
                )
            )
            if int(active_admins or 0) <= 1:
                raise ValueError("Cannot demote or deactivate the last active administrator.")
        if (
            next_role != user.role
            or next_person_id != user.person_id
            or (is_active is not None and bool(user.is_active) != is_active)
        ):
            user.session_version += 1
        user.role = next_role
        user.person_id = next_person_id
        if is_active is not None:
            user.is_active = int(is_active)
        session.flush()
        return _user_model_to_dict(user)


def _person_model_to_dict(person) -> dict[str, Any]:
    """Serialize a Person model without leaking SQLAlchemy implementation state."""
    from .dtos import PersonDTO

    # DTO validation is the only model-to-public boundary.  Convert the Boolean
    # back to the historic integer representation used by existing API clients.
    result = PersonDTO.model_validate(person).model_dump()
    result["active"] = int(result["active"])
    return result


def create_person(
    *,
    name: str,
    email: str | None = None,
    phone: str | None = None,
    role: str = Role.VOLUNTEER.value,
    active: bool = True,
    notes: str | None = None,
) -> dict[str, Any]:
    """Insert a contact through its repository and return its public mapping."""
    from .database import unit_of_work
    from .models import Person
    from .repositories import SQLAlchemyPersonRepository

    with unit_of_work() as session:
        person = SQLAlchemyPersonRepository(session).add(
            Person(
                name=name,
                email=email,
                phone=phone,
                role=role,
                active=int(active),
                notes=notes,
            )
        )
        return _person_model_to_dict(person)


def create_people(people: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Create an already validated batch of people atomically."""
    from .database import unit_of_work
    from .models import Person
    from .repositories import SQLAlchemyPersonRepository

    with unit_of_work() as session:
        models = [
            Person(
                name=person["name"],
                email=person.get("email"),
                phone=person.get("phone"),
                role=person.get("role", Role.VOLUNTEER.value),
                active=int(person.get("active", True)),
                notes=person.get("notes"),
            )
            for person in people
        ]
        created = SQLAlchemyPersonRepository(session).add_all(models)
        return [_person_model_to_dict(model) for model in created]


def list_people(
    *,
    limit: int | None = None,
    offset: int = 0,
    with_total: bool = False,
    search: str = "",
    person_id: int | None = None,
) -> list[dict[str, Any]] | tuple[list[dict[str, Any]], int]:
    """Return all contacts for staff and the selector controls."""
    from sqlalchemy import func, select

    from .database import unit_of_work
    from .models import Person

    with unit_of_work() as session:
        filters = () if person_id is None else (Person.id == person_id,)
        statement = select(Person).where(*filters).order_by(Person.id)
        if search:
            query = search.casefold()
            searchable = session.execute(
                select(
                    Person.id, Person.name, Person.email, Person.phone, Person.role, Person.active
                )
                .where(*filters)
                .order_by(Person.id)
            )
            ids = [
                values[0]
                for values in searchable
                if _matches_list_search(query, *values[:-1], "active" if values[-1] else "inactive")
            ]
            total = len(ids)
            if limit is not None:
                ids = ids[offset : offset + limit]
            statement = statement.where(Person.id.in_(ids))
        else:
            total = int(
                session.scalar(select(func.count()).select_from(Person).where(*filters)) or 0
            )
            if limit is not None:
                statement = statement.limit(limit).offset(offset)
        people = [_person_model_to_dict(person) for person in session.scalars(statement)]
        return (people, total) if with_total else people


def list_person_choices(person_id: int | None = None) -> list[dict[str, Any]]:
    """Return only person labels and identifiers for linking and displaying records."""
    from sqlalchemy import select

    from .database import unit_of_work
    from .models import Person

    with unit_of_work() as session:
        statement = select(Person.id, Person.name, Person.active).order_by(Person.id)
        if person_id is not None:
            statement = statement.where(Person.id == person_id)
        return [dict(row) for row in session.execute(statement).mappings()]


def get_person(person_id: int) -> dict[str, Any] | None:
    """Look up one person by primary key."""
    from .database import unit_of_work
    from .repositories import SQLAlchemyPersonRepository

    with unit_of_work() as session:
        person = SQLAlchemyPersonRepository(session).get(person_id)
        return _person_model_to_dict(person) if person is not None else None


def update_person(person_id: int, **updates: Any) -> dict[str, Any] | None:
    """Apply only recognized person fields and return the updated record."""
    if not updates:
        return get_person(person_id)

    allowed = {"name", "email", "phone", "role", "active", "notes"}
    updates_to_apply = {key: value for key, value in updates.items() if key in allowed}
    if not updates_to_apply:
        return get_person(person_id)

    from .database import unit_of_work
    from .repositories import SQLAlchemyPersonRepository

    with unit_of_work() as session:
        person = SQLAlchemyPersonRepository(session).get(person_id)
        if person is None:
            return None
        for field, value in updates_to_apply.items():
            setattr(person, field, int(value) if field == "active" else value)
        session.flush()
        return _person_model_to_dict(person)


def anonymize_person(person_id: int) -> bool:
    """Remove direct identifiers while preserving linked trip and financial history."""
    from sqlalchemy import select
    from .database import unit_of_work
    from .models import GiftAidDeclaration, User
    from .repositories import SQLAlchemyPersonRepository

    with unit_of_work() as session:
        person = SQLAlchemyPersonRepository(session).get(person_id)
        if person is None:
            return False
        replacement = f"Deleted person {person_id}"
        person.name = replacement
        person.email = person.phone = person.notes = None
        person.active = 0
        person.role = "other"
        for declaration in session.scalars(
            select(GiftAidDeclaration).where(GiftAidDeclaration.person_id == person_id)
        ):
            declaration.full_name = replacement
            declaration.home_address = declaration.postcode = declaration.evidence_ref = None
        for user in session.scalars(select(User).where(User.person_id == person_id)):
            user.is_active = 0
            user.person_id = None
            user.session_version += 1
        session.flush()
        return True


def delete_or_deactivate_person(person_id: int) -> str | None:
    """Delete an unused person or make a referenced person dormant.

    Financial, trip, declaration, and account relationships must retain their
    person key for audit integrity. A completely unused directory entry has no
    such history and can safely be removed instead of anonymised.

    The operation runs inside one SQLAlchemy unit of work.  Each ``count`` query
    asks the database whether the person's primary key is referenced by one of
    the protected record types.  Setting ``active`` to zero is a soft delete:
    the row and foreign keys remain available to reports, but normal active-only
    selectors omit it.  ``session.delete`` is used only when every count is zero.
    The unit-of-work context commits on success and rolls back on an exception.
    """
    from sqlalchemy import func, select
    from .database import unit_of_work
    from .models import Donation, GiftAidDeclaration, Payment, Person, Trip, User

    with unit_of_work() as session:
        person = session.get(Person, person_id)
        if person is None:
            return None
        # ``any`` short-circuits after the first relationship is found, avoiding
        # needless queries for a person that is already known to have history.
        related = any(
            session.scalar(select(func.count()).select_from(model).where(field == person_id))
            for model, field in (
                (Trip, Trip.person_id),
                (GiftAidDeclaration, GiftAidDeclaration.person_id),
                (Donation, Donation.person_id),
                (Payment, Payment.person_id),
                (User, User.person_id),
            )
        )
        if related:
            person.active = 0
            session.flush()
            return "dormant"
        session.delete(person)
        session.flush()
        return "deleted"


def _privacy_notice_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """Return a notice version only when its stored digest still matches its text."""
    if row is None:
        return None
    notice = dict(row)
    actual_hash = hashlib.sha256(notice["notice_text"].encode("utf-8")).hexdigest()
    if not hmac.compare_digest(actual_hash, notice["text_sha256"]):
        raise RuntimeError("Stored privacy notice text does not match its recorded SHA-256.")
    return notice


def get_current_privacy_notice() -> dict[str, Any]:
    """Return the most recently published immutable privacy notice version."""
    from .database import unit_of_work
    from .repositories import SQLAlchemyPrivacyNoticeRepository

    with unit_of_work() as session:
        model = SQLAlchemyPrivacyNoticeRepository(session).current()
        notice = _privacy_notice_model_to_dict(model) if model else None
        if notice is None:
            raise RuntimeError("No privacy notice version has been published.")
        return notice


def get_privacy_notice_version(version_id: int) -> dict[str, Any] | None:
    """Return one historical notice version by its stable identifier."""
    from .database import unit_of_work
    from .repositories import SQLAlchemyPrivacyNoticeRepository

    with unit_of_work() as session:
        model = SQLAlchemyPrivacyNoticeRepository(session).get(version_id)
        return _privacy_notice_model_to_dict(model) if model else None


def publish_privacy_notice(
    notice_text: str, *, effective_date: str | None = None
) -> dict[str, Any]:
    """Append a new notice version; database triggers prohibit later edits or deletion."""
    effective = date.fromisoformat(effective_date) if effective_date else date.today()
    digest = hashlib.sha256(notice_text.encode("utf-8")).hexdigest()
    from .database import unit_of_work
    from .models import PrivacyNoticeVersion
    from .repositories import SQLAlchemyPrivacyNoticeRepository

    with unit_of_work() as session:
        model = SQLAlchemyPrivacyNoticeRepository(session).add(
            PrivacyNoticeVersion(
                effective_date=effective.isoformat(),
                published_date=date.today().isoformat(),
                text_sha256=digest,
                notice_text=notice_text,
            )
        )
        return _privacy_notice_model_to_dict(model)


def _privacy_notice_model_to_dict(model) -> dict[str, Any]:
    """Verify and serialize an immutable privacy-notice model."""
    from .dtos import PrivacyNoticeVersionDTO

    notice = PrivacyNoticeVersionDTO.model_validate(model).model_dump()
    actual_hash = hashlib.sha256(notice["notice_text"].encode("utf-8")).hexdigest()
    if not hmac.compare_digest(actual_hash, notice["text_sha256"]):
        raise RuntimeError("Stored privacy notice text does not match its recorded SHA-256.")
    return notice


def create_declaration(
    *,
    person_id: int,
    full_name: str,
    home_address: str | None = None,
    postcode: str | None = None,
    declaration_date: str | None = None,
    scope: str = "future",
    withdrawn_date: str | None = None,
    evidence_ref: str | None = None,
    evidence_image_base64: str | None = None,
    evidence_image_mime: str | None = None,
    privacy_notice_version_id: int | None = None,
) -> dict[str, Any]:
    """Store Gift Aid declaration details for the selected donor."""
    from .database import unit_of_work
    from .models import GiftAidDeclaration
    from .repositories import SQLAlchemyDeclarationRepository, SQLAlchemyPrivacyNoticeRepository

    with unit_of_work() as session:
        notices = SQLAlchemyPrivacyNoticeRepository(session)
        notice = (
            notices.current()
            if privacy_notice_version_id is None
            else notices.get(privacy_notice_version_id)
        )
        if notice is None:
            raise ValueError("A valid privacy notice version is required.")
        model = SQLAlchemyDeclarationRepository(session).add(
            GiftAidDeclaration(
                person_id=person_id,
                full_name=full_name,
                home_address=home_address,
                postcode=postcode,
                declaration_date=declaration_date,
                scope=scope,
                withdrawn_date=withdrawn_date,
                evidence_ref=evidence_ref,
                evidence_image_base64=evidence_image_base64,
                evidence_image_mime=evidence_image_mime,
                has_evidence_image=int(bool(evidence_image_base64)),
                privacy_notice_version_id=notice.id,
                privacy_notice_sha256=notice.text_sha256,
                active=1,
            )
        )
        return _declaration_model_to_dict(model)


def get_declaration(declaration_id: int) -> dict[str, Any] | None:
    """Fetch one declaration, including logically deleted records."""
    from .database import unit_of_work
    from .repositories import SQLAlchemyDeclarationRepository

    with unit_of_work() as session:
        model = SQLAlchemyDeclarationRepository(session).get(declaration_id)
        return _declaration_model_to_dict(model) if model else None


def update_declaration(declaration_id: int, **fields: Any) -> dict[str, Any] | None:
    """Update declaration details and its logical-active flag."""
    allowed_fields = {
        "person_id",
        "full_name",
        "home_address",
        "postcode",
        "declaration_date",
        "scope",
        "withdrawn_date",
        "evidence_ref",
        "active",
    }
    updates = {key: value for key, value in fields.items() if key in allowed_fields}
    if "full_name" in updates and not str(updates["full_name"] or "").strip():
        raise ValueError("Full name is required.")
    if "person_id" in updates:
        try:
            updates["person_id"] = int(updates["person_id"])
        except (TypeError, ValueError) as exc:
            raise ValueError("A valid person is required.") from exc
    if "active" in updates:
        updates["active"] = int(bool(updates["active"]))

    from .database import unit_of_work
    from .repositories import SQLAlchemyDeclarationRepository

    with unit_of_work() as session:
        model = SQLAlchemyDeclarationRepository(session).get(declaration_id)
        if model is None:
            return None
        if "person_id" in updates and updates["person_id"] != model.person_id and model.donations:
            raise ValueError(
                "A declaration linked to donations cannot be reassigned to another person."
            )
        for field, value in updates.items():
            setattr(model, field, value)
        session.flush()
        return _declaration_model_to_dict(model)


def deactivate_declaration(declaration_id: int) -> bool:
    """Logically delete a declaration without removing evidence or donation links."""
    from .database import unit_of_work
    from .repositories import SQLAlchemyDeclarationRepository

    with unit_of_work() as session:
        model = SQLAlchemyDeclarationRepository(session).get(declaration_id)
        if model is None:
            return False
        model.active = 0
        session.flush()
        return True


def list_declarations(
    person_id: int | None = None,
    *,
    limit: int | None = None,
    offset: int = 0,
    with_total: bool = False,
    search: str = "",
) -> list[dict[str, Any]] | tuple[list[dict[str, Any]], int]:
    """List declarations, optionally scoped to one person."""
    from sqlalchemy import func, select
    from .database import unit_of_work
    from .models import GiftAidDeclaration

    with unit_of_work() as session:
        filters = () if person_id is None else (GiftAidDeclaration.person_id == person_id,)
        total = int(
            session.scalar(select(func.count()).select_from(GiftAidDeclaration).where(*filters))
            or 0
        )
        statement = select(GiftAidDeclaration).where(*filters).order_by(GiftAidDeclaration.id)
        if limit is not None and not search:
            statement = statement.limit(limit).offset(offset)
        declarations = [_declaration_model_to_dict(model) for model in session.scalars(statement)]
        if search:
            query = search.casefold()
            declarations = [
                item for item in declarations if query in json.dumps(item, default=str).casefold()
            ]
            total = len(declarations)
            if limit is not None:
                declarations = declarations[offset : offset + limit]
        return (declarations, total) if with_total else declarations


def _declaration_model_to_dict(model) -> dict[str, Any]:
    """Serialize declaration metadata without exposing confidential image data.

    The lightweight persisted flag tells clients whether to show the protected
    evidence endpoint.  It avoids loading and decrypting the deferred base64
    payload during list and detail queries.
    """
    from .dtos import DeclarationDTO

    result = DeclarationDTO.model_validate(model).model_dump()
    result["has_evidence_image"] = bool(model.has_evidence_image)
    result["active"] = int(bool(result["active"]))
    return result


def _destination_model_to_dict(model) -> dict[str, Any]:
    """Serialize a destination model through its public response DTO."""
    from .dtos import DestinationDTO

    result = DestinationDTO.model_validate(model).model_dump()
    result["active"] = int(result["active"])
    result["home_ground"] = int(result["home_ground"])
    return result


def create_destination(
    *,
    name: str,
    address: str | None = None,
    town_city: str | None = None,
    postcode: str | None = None,
    notes: str | None = None,
    home_ground: bool = False,
) -> dict[str, Any]:
    """Create a location and, when requested, move the home-ground flag to it."""
    cleaned_name = name.strip()
    if not cleaned_name:
        raise ValueError("Destination name is required.")
    from sqlalchemy import update
    from .database import unit_of_work
    from .models import Destination
    from .repositories import SQLAlchemyDestinationRepository

    with unit_of_work() as session:
        if home_ground:
            session.execute(
                update(Destination).where(Destination.home_ground == 1).values(home_ground=0)
            )
        model = SQLAlchemyDestinationRepository(session).add(
            Destination(
                name=cleaned_name,
                name_bidx=blind_index(cleaned_name),
                address=address,
                town_city=town_city,
                postcode=postcode,
                notes=notes,
                active=1,
                home_ground=int(home_ground),
            )
        )
        return _destination_model_to_dict(model)


def destination_import_conflicts(locations: list[dict[str, Any]]) -> dict[int, str]:
    """Resolve name conflicts by blind index without loading full location records."""
    from sqlalchemy import select
    from .database import unit_of_work
    from .models import Destination

    indexes = [blind_index(location["name"]) for location in locations]
    with unit_of_work() as session:
        existing = dict(
            session.execute(
                select(Destination.name_bidx, Destination.name).where(
                    Destination.name_bidx.in_(set(indexes))
                )
            ).all()
        )
    conflicts = {}
    seen = set()
    for index, name_index in enumerate(indexes):
        if name_index in existing:
            conflicts[index] = existing[name_index]
        elif name_index in seen:
            conflicts[index] = locations[index]["name"]
        seen.add(name_index)
    return conflicts


def create_destinations(locations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Create a validated set of locations atomically."""
    from sqlalchemy import update
    from .database import unit_of_work
    from .models import Destination
    from .repositories import SQLAlchemyDestinationRepository

    existing_conflicts = destination_import_conflicts(locations)
    if existing_conflicts:
        conflicts = "; ".join(f'"{name}"' for name in dict.fromkeys(existing_conflicts.values()))
        raise ValueError(f"Location name conflict(s): {conflicts}.")

    with unit_of_work() as session:
        if any(location.get("home_ground") for location in locations):
            session.execute(
                update(Destination).where(Destination.home_ground == 1).values(home_ground=0)
            )
        models = []
        for location in locations:
            models.append(
                Destination(
                    name=location["name"],
                    name_bidx=blind_index(location["name"]),
                    address=location.get("address"),
                    town_city=location.get("town_city"),
                    postcode=location.get("postcode"),
                    notes=location.get("notes"),
                    active=1,
                    home_ground=int(bool(location.get("home_ground"))),
                )
            )
        created = SQLAlchemyDestinationRepository(session).add_all(models)
        return [_destination_model_to_dict(model) for model in created]


def list_destinations(
    *,
    active_only: bool = False,
    limit: int | None = None,
    offset: int = 0,
    with_total: bool = False,
    search: str = "",
) -> list[dict[str, Any]] | tuple[list[dict[str, Any]], int]:
    """List locations; volunteers receive active locations only for trip choices."""
    from sqlalchemy import func, select
    from .database import unit_of_work
    from .models import Destination

    with unit_of_work() as session:
        filters = (Destination.active == 1,) if active_only else ()
        statement = select(
            Destination.id,
            Destination.name,
            Destination.address,
            Destination.town_city,
            Destination.postcode,
            Destination.notes,
            Destination.home_ground,
            Destination.active,
        ).where(*filters)

        def serialize(row) -> dict[str, Any]:
            """Convert one location projection into the representation used by clients.

            The query excludes internal ORM state and this adapter normalizes Boolean
            flags to the integer form retained for backward API compatibility.
            """
            return {
                "name": row.name,
                "address": row.address,
                "town_city": row.town_city,
                "postcode": row.postcode,
                "notes": row.notes,
                "home_ground": int(bool(row.home_ground)),
                "id": row.id,
                "active": int(bool(row.active)),
            }

        query = search.casefold()
        if query:
            rows = session.execute(
                select(
                    Destination.id,
                    Destination.name,
                    Destination.address,
                    Destination.town_city,
                    Destination.postcode,
                    Destination.active,
                    Destination.home_ground,
                ).where(*filters)
            )
            names = [
                (row.id, row.name)
                for row in rows
                if _matches_list_search(
                    query,
                    row.name,
                    row.address,
                    row.town_city,
                    row.postcode,
                    "active" if row.active else "inactive",
                    "home" if row.home_ground else "",
                )
            ]
            ids = [
                record[0]
                for record in sorted(names, key=lambda item: (item[1].casefold(), item[0]))
            ]
            total = len(ids)
            if limit is not None:
                ids = ids[offset : offset + limit]
            if not ids:
                return ([], total) if with_total else []
            records = {
                row.id: serialize(row)
                for row in session.execute(statement.where(Destination.id.in_(ids)))
            }
            destinations = [records[location_id] for location_id in ids if location_id in records]
        else:
            total = (
                int(
                    session.scalar(select(func.count()).select_from(Destination).where(*filters))
                    or 0
                )
                if with_total
                else 0
            )
            statement = statement.order_by(Destination.id)
            if limit is not None:
                statement = statement.limit(limit).offset(offset)
            destinations = [serialize(row) for row in session.execute(statement)]
        return (destinations, total) if with_total else destinations


def list_destination_choices() -> list[dict[str, Any]]:
    """Return searchable fields for every active trip-location choice.

    This deliberately uses a narrow SQLAlchemy ``select`` rather than loading
    complete ``Destination`` objects.  The browser needs only the identifier,
    display/search text, and home-ground flag, so notes and other columns are not
    decrypted or transferred.  ``session.execute`` returns row mappings that
    are converted into plain dictionaries suitable for Flask's JSON response.
    """
    from sqlalchemy import select

    from .database import unit_of_work
    from .models import Destination

    with unit_of_work() as session:
        rows = session.execute(
            select(
                Destination.id,
                Destination.name,
                Destination.town_city,
                Destination.postcode,
                Destination.active,
                Destination.home_ground,
            ).where(Destination.active == 1)
        ).mappings()
        return sorted(
            (dict(row) for row in rows), key=lambda row: (row["name"].casefold(), row["id"])
        )


def get_destination(destination_id: int) -> dict[str, Any] | None:
    """Retrieve a location, including cached coordinates used by routing."""
    from .database import unit_of_work
    from .repositories import SQLAlchemyDestinationRepository

    with unit_of_work() as session:
        model = SQLAlchemyDestinationRepository(session).get(destination_id)
        return _destination_model_to_dict(model) if model else None


def _route_location_fingerprint(destination: dict[str, Any]) -> str:
    """Fingerprint only fields whose changes can alter geocoding or routing."""
    values = [
        destination.get(field)
        for field in (
            "id",
            "active",
            "name",
            "address",
            "town_city",
            "postcode",
            "latitude",
            "longitude",
        )
    ]
    return hashlib.sha256(json.dumps(values, default=str).encode("utf-8")).hexdigest()


def get_cached_route_miles(
    origin_id: int, destination_id: int, *, now: int | None = None
) -> float | None:
    """Read a completed route shared by imports, invalidating stale location data."""
    from .database import unit_of_work
    from .models import Destination, RouteLookupCache

    current_time = int(datetime.now().timestamp()) if now is None else int(now)
    key = f"{origin_id}:{destination_id}"
    with unit_of_work() as session:
        cached = session.get(RouteLookupCache, key)
        if cached is None:
            return None
        origin = session.get(Destination, origin_id)
        destination = session.get(Destination, destination_id)
        if origin is None or destination is None or cached.expires_at <= current_time:
            session.delete(cached)
            return None
        if cached.origin_fingerprint != _route_location_fingerprint(
            _destination_model_to_dict(origin)
        ) or cached.destination_fingerprint != _route_location_fingerprint(
            _destination_model_to_dict(destination)
        ):
            session.delete(cached)
            return None
        return float(cached.distance_miles)


def save_cached_route_miles(
    origin_id: int,
    destination_id: int,
    distance_miles: float,
    *,
    now: int | None = None,
    ttl_seconds: int = 3600,
) -> None:
    """Upsert a successful route result for reuse by later imports and processes."""
    from .database import unit_of_work
    from .models import Destination, RouteLookupCache

    current_time = int(datetime.now().timestamp()) if now is None else int(now)
    key = f"{origin_id}:{destination_id}"
    with unit_of_work() as session:
        origin = session.get(Destination, origin_id)
        destination = session.get(Destination, destination_id)
        if origin is None or destination is None:
            return
        values = {
            "origin_id": origin_id,
            "destination_id": destination_id,
            "origin_fingerprint": _route_location_fingerprint(_destination_model_to_dict(origin)),
            "destination_fingerprint": _route_location_fingerprint(
                _destination_model_to_dict(destination)
            ),
            "distance_miles": str(distance_miles),
            "expires_at": current_time + ttl_seconds,
        }
        cached = session.get(RouteLookupCache, key)
        if cached is None:
            session.add(RouteLookupCache(key=key, **values))
        else:
            for field, value in values.items():
                setattr(cached, field, value)


def save_destination_coordinates(destination_id: int, latitude: float, longitude: float) -> None:
    """Persist geocoded coordinates so later route requests can skip geocoding."""
    from .database import unit_of_work
    from .repositories import SQLAlchemyDestinationRepository

    with unit_of_work() as session:
        model = SQLAlchemyDestinationRepository(session).get(destination_id)
        if model:
            model.latitude, model.longitude = str(latitude), str(longitude)
            session.flush()


def update_destination(destination_id: int, **updates: Any) -> dict[str, Any] | None:
    """Update allowed location fields and enforce the single active home-ground rule."""
    allowed = {"name", "address", "town_city", "postcode", "notes", "active", "home_ground"}
    updates_to_apply = {key: value for key, value in updates.items() if key in allowed}
    if "name" in updates_to_apply:
        updates_to_apply["name"] = str(updates_to_apply["name"]).strip()
        if not updates_to_apply["name"]:
            raise ValueError("Destination name is required.")
    if "active" in updates_to_apply:
        updates_to_apply["active"] = int(bool(updates_to_apply["active"]))
    if "home_ground" in updates_to_apply:
        updates_to_apply["home_ground"] = int(bool(updates_to_apply["home_ground"]))
    if updates_to_apply.get("active") == 0 and "home_ground" not in updates_to_apply:
        # Dormant locations cannot remain the default origin.
        updates_to_apply["home_ground"] = 0
    if updates_to_apply.get("home_ground") and updates_to_apply.get("active") == 0:
        raise ValueError("An inactive location cannot be the club home ground.")
    if not updates_to_apply:
        return get_destination(destination_id)
    from sqlalchemy import update
    from .database import unit_of_work
    from .models import Destination
    from .repositories import SQLAlchemyDestinationRepository

    with unit_of_work() as session:
        repository = SQLAlchemyDestinationRepository(session)
        model = repository.get(destination_id)
        if model is None:
            return None
        effective_active = updates_to_apply.get("active", model.active)
        if updates_to_apply.get("home_ground") and not effective_active:
            raise ValueError("An inactive location cannot be the club home ground.")
        if updates_to_apply.get("home_ground"):
            session.execute(
                update(Destination).where(Destination.home_ground == 1).values(home_ground=0)
            )
        for field, value in updates_to_apply.items():
            setattr(model, field, value)
        if "name" in updates_to_apply:
            model.name_bidx = blind_index(updates_to_apply["name"])
        if {"name", "address", "town_city", "postcode"} & updates_to_apply.keys():
            model.latitude = model.longitude = None
        session.flush()
        return _destination_model_to_dict(model)


def delete_destination(destination_id: int) -> bool:
    """Delete an unused location; historical trip references require deactivation."""
    from sqlalchemy import or_, select
    from .database import unit_of_work
    from .models import Trip
    from .repositories import SQLAlchemyDestinationRepository

    with unit_of_work() as session:
        repository = SQLAlchemyDestinationRepository(session)
        model = repository.get(destination_id)
        if model is None:
            return False
        if (
            session.scalar(
                select(Trip.id)
                .where(
                    or_(
                        Trip.destination_id == destination_id,
                        Trip.origin_id == destination_id,
                    )
                )
                .limit(1)
            )
            is not None
        ):
            raise ValueError("Location is used by trip records; deactivate it instead.")
        repository.delete(model)
        return True


def get_trip_import_people(person_ids: set[int]) -> set[int]:
    """Check person references in bounded batches without decrypting contact details."""
    from sqlalchemy import select

    from .database import unit_of_work
    from .models import Person

    ids = sorted(person_ids)
    with unit_of_work() as session:
        return {
            person_id
            for start in range(0, len(ids), 500)
            for person_id in session.scalars(
                select(Person.id).where(Person.id.in_(ids[start : start + 500]))
            )
        }


def get_trip_import_locations(location_ids: set[int]) -> set[int]:
    """Check which referenced locations are active without reading encrypted fields."""
    from sqlalchemy import select

    from .database import unit_of_work
    from .models import Destination

    ids = sorted(location_ids)
    with unit_of_work() as session:
        return {
            location_id
            for start in range(0, len(ids), 500)
            for location_id in session.scalars(
                select(Destination.id).where(
                    Destination.id.in_(ids[start : start + 500]), Destination.active == 1
                )
            )
        }


def create_trip(
    *,
    person_id: int,
    origin_id: int | None = None,
    destination_id: int | None = None,
    vehicle_type: str,
    trip_date: date | str,
    miles_one_way: float,
    return_trip: bool,
    passengers: int = 0,
    declaration_accepted: bool = True,
    declaration_text: str | None = None,
    tax_year: str | None = None,
    status: str = "submitted",
    request_token: str | None = None,
) -> dict[str, Any]:
    """Validate and insert a trip, computing its round-trip mileage and tax year."""
    return create_trips(
        [
            {
                "person_id": person_id,
                "origin_id": origin_id,
                "destination_id": destination_id,
                "vehicle_type": vehicle_type,
                "trip_date": trip_date,
                "miles_one_way": miles_one_way,
                "return_trip": return_trip,
                "passengers": passengers,
                "declaration_accepted": declaration_accepted,
                "tax_year": tax_year,
                "status": status,
                "request_token": request_token,
            }
        ],
        declaration_text=declaration_text,
    )[0]


def create_trips(
    trips: list[dict[str, Any]],
    *,
    declaration_text: str | None,
) -> list[dict[str, Any]]:
    """Validate and insert multiple trips in one database transaction."""
    prepared = []
    for trip in trips:
        trip_date = trip["trip_date"]
        if isinstance(trip_date, str):
            trip_date = date.fromisoformat(trip_date)
        validate_declaration(trip.get("declaration_accepted", True), declaration_text)
        miles_one_way = float(trip["miles_one_way"])
        return_trip = bool(trip.get("return_trip", False))
        prepared.append(
            {
                "person_id": int(trip["person_id"]),
                "origin_id": trip.get("origin_id"),
                "destination_id": trip.get("destination_id"),
                "vehicle_type": trip["vehicle_type"],
                "trip_date": trip_date,
                "tax_year": trip.get("tax_year") or calculate_tax_year(trip_date),
                "miles_one_way": miles_one_way,
                "return_trip": return_trip,
                "total_miles": miles_one_way * (2 if return_trip else 1),
                "passengers": int(trip.get("passengers", 0)),
                "declaration_accepted": bool(trip.get("declaration_accepted", True)),
                "status": trip.get("status", "submitted"),
                "request_token": trip.get("request_token"),
            }
        )

    from sqlalchemy import select
    from .database import unit_of_work
    from .models import Trip
    from .repositories import SQLAlchemyDestinationRepository, SQLAlchemyTripRepository

    with unit_of_work() as session:
        if len(prepared) == 1 and prepared[0].get("request_token"):
            existing = session.scalar(
                select(Trip).where(Trip.request_token == prepared[0]["request_token"])
            )
            if existing is not None:
                return [_trip_model_to_dict(existing)]
        destinations = SQLAlchemyDestinationRepository(session)
        models = []
        for trip in prepared:
            destination = (
                destinations.get(trip["destination_id"]) if trip["destination_id"] else None
            )
            if trip["destination_id"] is not None and (
                destination is None or not destination.active
            ):
                raise ValueError("Destination was not found or is inactive.")
            origin = destinations.get(trip["origin_id"]) if trip["origin_id"] else None
            if trip["origin_id"] is not None and (origin is None or not origin.active):
                raise ValueError("Origin location was not found or is inactive.")
            models.append(
                Trip(
                    person_id=trip["person_id"],
                    origin_id=trip["origin_id"],
                    destination_id=trip["destination_id"],
                    vehicle_type=trip["vehicle_type"],
                    trip_date=trip["trip_date"].isoformat(),
                    tax_year=trip["tax_year"],
                    miles_one_way=str(trip["miles_one_way"]),
                    return_trip=int(trip["return_trip"]),
                    total_miles=str(trip["total_miles"]),
                    passengers=str(trip["passengers"]),
                    declaration_accepted=int(trip["declaration_accepted"]),
                    declaration_text=declaration_text
                    or "I declare that I have taken other parents' children on this journey.",
                    declared_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    status=trip["status"],
                    request_token=trip.get("request_token"),
                )
            )
        created = SQLAlchemyTripRepository(session).add_all(models)
        return [_trip_model_to_dict(model) for model in created]


def _trip_model_to_dict(model) -> dict[str, Any]:
    """Serialize a trip model and its loaded location/payment relationships."""
    from .dtos import TripDTO

    result = TripDTO.model_validate(model).model_dump()
    result["return_trip"] = int(result["return_trip"])
    result["declaration_accepted"] = int(result["declaration_accepted"])
    result["destination_name"] = model.destination.name if model.destination else None
    result["origin_name"] = model.origin.name if model.origin else None
    result["payment_id"] = model.payment_links[0].payment_id if model.payment_links else None
    return result


def list_trips(
    person_id: int | None = None,
    *,
    limit: int | None = None,
    offset: int = 0,
    with_total: bool = False,
    unpaid_only: bool = False,
    search: str = "",
) -> list[dict[str, Any]] | tuple[list[dict[str, Any]], int]:
    """List trip records with readable endpoints and any linked reimbursement ID."""
    from sqlalchemy import func, select
    from sqlalchemy.orm import selectinload
    from .database import unit_of_work
    from .models import Trip

    with unit_of_work() as session:
        filters = [] if person_id is None else [Trip.person_id == person_id]
        if unpaid_only:
            filters.append(~Trip.payment_links.any())
        total = int(session.scalar(select(func.count()).select_from(Trip).where(*filters)) or 0)
        statement = (
            select(Trip)
            .where(*filters)
            .options(
                selectinload(Trip.origin),
                selectinload(Trip.destination),
                selectinload(Trip.payment_links),
            )
            .order_by(Trip.trip_date.desc())
        )
        if limit is not None and not search:
            statement = statement.limit(limit).offset(offset)
        trips = [_trip_model_to_dict(model) for model in session.scalars(statement)]
        if search:
            query = search.casefold()
            trips = [item for item in trips if query in json.dumps(item, default=str).casefold()]
            total = len(trips)
            if limit is not None:
                trips = trips[offset : offset + limit]
        return (trips, total) if with_total else trips


def get_trip(trip_id: int) -> dict[str, Any] | None:
    """Fetch one trip with location names and its linked payment, if any."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from .database import unit_of_work
    from .models import Trip

    with unit_of_work() as session:
        model = session.scalar(
            select(Trip)
            .where(Trip.id == trip_id)
            .options(
                selectinload(Trip.origin),
                selectinload(Trip.destination),
                selectinload(Trip.payment_links),
            )
        )
        return _trip_model_to_dict(model) if model else None


def update_trip(trip_id: int, **updates: Any) -> dict[str, Any] | None:
    """Update an unpaid trip and derive any dependent values from its inputs."""
    if not updates:
        return get_trip(trip_id)

    allowed = {
        "person_id",
        "origin_id",
        "destination_id",
        "vehicle_type",
        "trip_date",
        "miles_one_way",
        "return_trip",
        "passengers",
        "status",
    }
    updates_to_apply = {key: value for key, value in updates.items() if key in allowed}
    if "person_id" in updates_to_apply:
        updates_to_apply["person_id"] = int(updates_to_apply["person_id"])
    if not updates_to_apply:
        return get_trip(trip_id)

    from .database import unit_of_work
    from .repositories import SQLAlchemyDestinationRepository, SQLAlchemyTripRepository

    with unit_of_work() as session:
        model = SQLAlchemyTripRepository(session).get(trip_id)
        if model is None:
            return None
        if model.payment_links:
            raise ValueError("A paid trip is locked. Record a correction note instead.")
        destinations = SQLAlchemyDestinationRepository(session)
        for field in ("origin_id", "destination_id"):
            location_id = updates_to_apply.get(field)
            location = destinations.get(location_id) if location_id is not None else None
            if location_id is not None and (location is None or not location.active):
                raise ValueError(
                    f"{field.replace('_', ' ').capitalize()} was not found or is inactive."
                )
        for field, value in updates_to_apply.items():
            if field == "trip_date" and isinstance(value, date):
                value = value.isoformat()
            setattr(model, field, str(value) if field in ENCRYPTED_FIELDS["trips"] else value)
        if "trip_date" in updates_to_apply:
            model.tax_year = calculate_tax_year(date.fromisoformat(model.trip_date))
        if {"miles_one_way", "return_trip"} & updates_to_apply.keys():
            model.total_miles = str(float(model.miles_one_way) * (2 if model.return_trip else 1))
        session.flush()
        return _trip_model_to_dict(model)


def _trip_correction_to_dict(model) -> dict[str, Any]:
    """Expose a correction to authorized staff without including account details."""
    return {
        "id": model.id,
        "trip_id": model.trip_id,
        "author_user_id": model.author_user_id,
        "note": model.note,
        "created_at": model.created_at,
    }


def create_trip_correction(trip_id: int, *, author_user_id: int, note: str) -> dict | None:
    """Append a correction to a paid trip and preserve the original reimbursement."""
    from .database import unit_of_work
    from .models import Trip, TripCorrection

    with unit_of_work() as session:
        trip = session.get(Trip, trip_id)
        if trip is None:
            return None
        if not trip.payment_links:
            raise ValueError("Correction notes are for paid trips.")
        correction = TripCorrection(trip_id=trip_id, author_user_id=author_user_id, note=note)
        session.add(correction)
        session.flush()
        return _trip_correction_to_dict(correction)


def list_trip_corrections(trip_id: int) -> list[dict]:
    """List paid-trip correction evidence in the order staff recorded it."""
    from sqlalchemy import select

    from .database import unit_of_work
    from .models import TripCorrection

    with unit_of_work() as session:
        corrections = session.scalars(
            select(TripCorrection)
            .where(TripCorrection.trip_id == trip_id)
            .order_by(TripCorrection.id)
        )
        return [_trip_correction_to_dict(row) for row in corrections]


def delete_trip(trip_id: int) -> bool:
    """Delete an unpaid trip; linked trips are protected as payment evidence."""
    from .database import unit_of_work
    from .repositories import SQLAlchemyTripRepository

    with unit_of_work() as session:
        repository = SQLAlchemyTripRepository(session)
        model = repository.get(trip_id)
        if model is None:
            return False
        if model.payment_links:
            raise ValueError("A trip linked to a payment cannot be deleted.")
        repository.delete(model)
        return True


def create_donation(
    *,
    person_id: int,
    amount_pence: int,
    donated_date: str | None = None,
    method: str | None = None,
    reference: str | None = None,
    declaration_id: int | None = None,
    gift_aid_eligible: bool = False,
    gift_aid_claimed_date: str | None = None,
    linked_payment_id: int | None = None,
    linked_payment_ids: Iterable[int] | None = None,
    received_date: str | None = None,
    bank_reference: str | None = None,
    receipt_id: int | None = None,
) -> dict[str, Any]:
    """Create a positive donation and validate its optional same-person links."""
    payment_ids = _normalize_payment_ids(linked_payment_ids, linked_payment_id)
    from .database import unit_of_work
    from .models import Donation, DonationPayment
    from .repositories import (
        SQLAlchemyDeclarationRepository,
        SQLAlchemyDonationPaymentRepository,
        SQLAlchemyDonationRepository,
        SQLAlchemyPersonRepository,
    )

    with unit_of_work() as session:
        if amount_pence <= 0:
            raise ValueError("Donation amount must be greater than zero.")
        if SQLAlchemyPersonRepository(session).get(person_id) is None:
            raise ValueError("Person was not found.")
        _validate_donation_payment_links(session, payment_ids, person_id)
        declaration = (
            SQLAlchemyDeclarationRepository(session).get(declaration_id) if declaration_id else None
        )
        if declaration_id is not None and (
            declaration is None or declaration.person_id != person_id
        ):
            raise ValueError("Linked declaration must belong to the same person.")
        model = SQLAlchemyDonationRepository(session).add(
            Donation(
                person_id=person_id,
                amount_pence=str(amount_pence),
                donated_date=donated_date,
                method=method,
                reference=reference,
                declaration_id=declaration_id,
                gift_aid_eligible=int(gift_aid_eligible),
                gift_aid_claimed_date=gift_aid_claimed_date,
                linked_payment_id=payment_ids[0] if payment_ids else None,
                received_date=received_date,
                bank_reference=bank_reference,
                receipt_id=str(receipt_id) if receipt_id is not None else None,
                active=1,
            )
        )
        SQLAlchemyDonationPaymentRepository(session).add_all(
            [
                DonationPayment(donation_id=model.id, payment_id=payment_id)
                for payment_id in payment_ids
            ]
        )
        return _donation_model_to_dict(model)


def _normalize_payment_ids(
    linked_payment_ids: Iterable[int] | None,
    linked_payment_id: int | None = None,
) -> list[int]:
    """Normalize legacy single and current multiple payment-link inputs."""
    values = (
        linked_payment_ids
        if linked_payment_ids is not None
        else ([] if linked_payment_id in (None, "") else [linked_payment_id])
    )
    if isinstance(values, (str, bytes)):
        raise ValueError("Linked reimbursement payments must be provided as a list of IDs.")
    try:
        payment_ids = [int(value) for value in values if value not in (None, "")]
    except (TypeError, ValueError) as exc:
        raise ValueError("Linked reimbursement payments must be valid payment IDs.") from exc
    if any(payment_id <= 0 for payment_id in payment_ids):
        raise ValueError("Linked reimbursement payments must be valid payment IDs.")
    if len(payment_ids) != len(set(payment_ids)):
        raise ValueError("A payment can only be linked once to a donation.")
    return payment_ids


def _validate_donation_payment_links(
    session,
    payment_ids: list[int],
    person_id: int,
) -> None:
    """Require every related payment to belong to the donation's person."""
    if not payment_ids:
        return
    from sqlalchemy import select
    from .models import Payment

    payments = list(session.scalars(select(Payment).where(Payment.id.in_(payment_ids))))
    if len(payments) != len(payment_ids) or any(
        payment.person_id != person_id for payment in payments
    ):
        raise ValueError("Linked reimbursement payments must belong to the same person.")


def _donation_model_to_dict(model, *, created_by=None, created_at=None) -> dict[str, Any]:
    """Serialize a donation model with ordered payment links and audit metadata."""
    from .dtos import DonationDTO

    donation = DonationDTO.model_validate(model).model_dump()
    payment_ids = sorted(link.payment_id for link in model.payment_links)
    donation["linked_payment_ids"] = payment_ids
    donation["linked_payment_id"] = payment_ids[0] if payment_ids else None
    donation["gift_aid_eligible"] = int(donation["gift_aid_eligible"])
    donation["active"] = int(donation["active"])
    if created_by is not None or created_at is not None:
        donation["created_by"] = created_by
        donation["created_at"] = created_at
    return donation


def update_donation(donation_id: int, **updates: Any) -> dict[str, Any] | None:
    """Edit donation details or active state, validating links after person changes."""
    allowed = {
        "person_id",
        "amount_pence",
        "donated_date",
        "method",
        "reference",
        "declaration_id",
        "gift_aid_eligible",
        "gift_aid_claimed_date",
        "linked_payment_id",
        "linked_payment_ids",
        "received_date",
        "bank_reference",
        "receipt_id",
        "active",
    }
    updates_to_apply = {key: value for key, value in updates.items() if key in allowed}
    payment_ids_supplied = (
        "linked_payment_ids" in updates_to_apply or "linked_payment_id" in updates_to_apply
    )
    if payment_ids_supplied:
        payment_ids = _normalize_payment_ids(
            updates_to_apply.get("linked_payment_ids"),
            updates_to_apply.get("linked_payment_id"),
        )
        updates_to_apply.pop("linked_payment_ids", None)
        updates_to_apply.pop("linked_payment_id", None)
    else:
        payment_ids = None
    if "amount_pence" in updates_to_apply:
        updates_to_apply["amount_pence"] = int(updates_to_apply["amount_pence"])
        if updates_to_apply["amount_pence"] <= 0:
            raise ValueError("Donation amount must be greater than zero.")
    if "person_id" in updates_to_apply:
        updates_to_apply["person_id"] = int(updates_to_apply["person_id"])
    if "declaration_id" in updates_to_apply and updates_to_apply["declaration_id"] in ("", None):
        updates_to_apply["declaration_id"] = None
    elif "declaration_id" in updates_to_apply:
        updates_to_apply["declaration_id"] = int(updates_to_apply["declaration_id"])
    if "receipt_id" in updates_to_apply and updates_to_apply["receipt_id"] in ("", None):
        updates_to_apply["receipt_id"] = None
    elif "receipt_id" in updates_to_apply:
        updates_to_apply["receipt_id"] = int(updates_to_apply["receipt_id"])
    for field in ("gift_aid_eligible", "active"):
        if field in updates_to_apply:
            # SQLite stores booleans as integer 0/1.
            updates_to_apply[field] = int(bool(updates_to_apply[field]))
    if not updates_to_apply and payment_ids is None:
        return get_donation(donation_id)

    from .database import unit_of_work
    from .models import DonationPayment
    from .repositories import (
        SQLAlchemyDeclarationRepository,
        SQLAlchemyDonationPaymentRepository,
        SQLAlchemyDonationRepository,
        SQLAlchemyPersonRepository,
    )

    with unit_of_work() as session:
        model = SQLAlchemyDonationRepository(session).get(donation_id)
        if model is None:
            return None
        person_id = updates_to_apply.get("person_id", model.person_id)
        if SQLAlchemyPersonRepository(session).get(person_id) is None:
            raise ValueError("Person was not found.")
        payment_ids = (
            [link.payment_id for link in model.payment_links]
            if payment_ids is None
            else payment_ids
        )
        _validate_donation_payment_links(session, payment_ids, person_id)
        declaration_id = updates_to_apply.get("declaration_id", model.declaration_id)
        declaration = (
            SQLAlchemyDeclarationRepository(session).get(declaration_id) if declaration_id else None
        )
        if declaration_id is not None and (
            declaration is None or declaration.person_id != person_id
        ):
            raise ValueError("Linked declaration must belong to the same person.")
        if payment_ids_supplied:
            updates_to_apply["linked_payment_id"] = payment_ids[0] if payment_ids else None
        for field, value in updates_to_apply.items():
            if field in ENCRYPTED_FIELDS["donations"] and value is not None:
                value = str(value)
            setattr(model, field, value)
        if payment_ids_supplied:
            for link in list(model.payment_links):
                session.delete(link)
            session.flush()
            SQLAlchemyDonationPaymentRepository(session).add_all(
                [
                    DonationPayment(donation_id=model.id, payment_id=payment_id)
                    for payment_id in payment_ids
                ]
            )
            session.expire(model, ["payment_links"])
        session.flush()
        return _donation_model_to_dict(model)


def get_donation(donation_id: int) -> dict[str, Any] | None:
    """Fetch a donation by ID, including dormant entries."""
    from .database import unit_of_work
    from .repositories import SQLAlchemyDonationRepository

    with unit_of_work() as session:
        model = SQLAlchemyDonationRepository(session).get(donation_id)
        return _donation_model_to_dict(model) if model else None


def set_donation_active(donation_id: int, active: bool) -> dict[str, Any] | None:
    """Toggle the donation's logical-delete flag without removing the row."""
    return update_donation(donation_id, active=active)


def list_donations(
    person_id: int | None = None,
    *,
    limit: int | None = None,
    offset: int = 0,
    with_total: bool = False,
    search: str = "",
) -> list[dict[str, Any]] | tuple[list[dict[str, Any]], int]:
    """List all donations and attach creator metadata when audit history exists."""
    from sqlalchemy import func, select
    from sqlalchemy.orm import selectinload
    from .database import unit_of_work
    from .models import AuditEvent, Donation

    with unit_of_work() as session:
        filters = () if person_id is None else (Donation.person_id == person_id,)
        total = int(session.scalar(select(func.count()).select_from(Donation).where(*filters)) or 0)
        statement = (
            select(Donation)
            .where(*filters)
            .options(selectinload(Donation.payment_links))
            .order_by(
                Donation.donated_date.desc(),
                Donation.id.desc(),
            )
        )
        if limit is not None and not search:
            statement = statement.limit(limit).offset(offset)
        donations = []
        for model in session.scalars(statement):
            event = session.scalar(
                select(AuditEvent)
                .where(
                    AuditEvent.entity_type == "donation",
                    AuditEvent.entity_id == str(model.id),
                    AuditEvent.action == "created",
                )
                .order_by(AuditEvent.id)
                .limit(1)
            )
            donations.append(
                _donation_model_to_dict(
                    model,
                    created_by=event.actor_username if event else None,
                    created_at=event.occurred_at if event else None,
                )
            )
        if search:
            query = search.casefold()
            donations = [
                item for item in donations if query in json.dumps(item, default=str).casefold()
            ]
            total = len(donations)
            if limit is not None:
                donations = donations[offset : offset + limit]
        return (donations, total) if with_total else donations


def _payment_trip_quote(
    session,
    person_id: int,
    trip_ids: Iterable[int],
    *,
    require_unpaid: bool = True,
) -> dict[str, Any]:
    """Calculate trip expenses and optionally enforce payment eligibility.

    Payment creation requires every selected trip to be unpaid. Reports reuse
    the same calculation for display but may include trips whose reimbursement
    has already been recorded, so callers explicitly choose whether the
    eligibility guard applies.
    """
    from bisect import bisect_right

    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from .models import MileageRateSchedule, Trip

    selected = list(trip_ids)
    if len(set(selected)) != len(selected):
        raise ValueError("A trip can only be selected once for a payment.")
    if len(selected) > 10000 or any(not 0 < trip_id <= 2**63 - 1 for trip_id in selected):
        raise ValueError("Select up to 10,000 valid trip IDs for a payment.")
    trips = []
    for start in range(0, len(selected), 500):
        trips.extend(
            session.scalars(
                select(Trip)
                .where(Trip.person_id == person_id, Trip.id.in_(selected[start : start + 500]))
                .options(
                    selectinload(Trip.payment_links),
                    selectinload(Trip.origin),
                    selectinload(Trip.destination),
                )
            )
        )
    if len(trips) != len(selected):
        raise ValueError("Every linked trip must belong to the same person as the payment.")
    if require_unpaid and any(trip.payment_links for trip in trips):
        raise ValueError("A selected trip has already been linked to a payment.")

    schedules = (
        [
            _rate_model_to_dict(model)
            for model in session.scalars(
                select(MileageRateSchedule).order_by(MileageRateSchedule.effective_from)
            )
        ]
        if trips
        else []
    )
    schedule_dates = [rate["effective_from"] for rate in schedules]
    selected_by_id = {trip.id: trip for trip in trips}
    accumulated_car_miles: dict[str, float] = {}
    business_miles_before_trip: dict[int, float] = {}
    all_person_trips = session.execute(
        select(
            Trip.id,
            Trip.trip_date,
            Trip.tax_year,
            Trip.vehicle_type,
            Trip.total_miles,
        )
        .where(Trip.person_id == person_id)
        .order_by(Trip.trip_date, Trip.id)
    )
    for trip_id, trip_date, stored_tax_year, vehicle_type, total_miles in all_person_trips:
        tax_year = stored_tax_year or calculate_tax_year(date.fromisoformat(trip_date))
        miles_before = accumulated_car_miles.get(tax_year, 0.0)
        if trip_id in selected_by_id:
            business_miles_before_trip[trip_id] = miles_before
        if vehicle_type == "car":
            accumulated_car_miles[tax_year] = miles_before + float(total_miles)

    breakdowns = []
    for trip in sorted(trips, key=lambda item: (item.trip_date, item.id)):
        schedule_index = bisect_right(schedule_dates, trip.trip_date) - 1
        if schedule_index < 0:
            raise ValueError(f"No mileage rate schedule applies on {trip.trip_date}.")
        schedule = schedules[schedule_index]
        calculation = calculate_trip_amounts(
            vehicle_type=trip.vehicle_type,
            trip_date=date.fromisoformat(trip.trip_date),
            miles_one_way=float(trip.miles_one_way),
            return_trip=bool(trip.return_trip),
            passengers=int(trip.passengers),
            total_business_miles=business_miles_before_trip[trip.id],
            rate_bands=rate_schedule_to_bands(schedule),
        )
        breakdowns.append(
            {
                "trip_id": trip.id,
                "trip_date": trip.trip_date,
                "vehicle_type": trip.vehicle_type,
                "return_trip": bool(trip.return_trip),
                "origin": trip.origin.name if trip.origin else None,
                "destination": trip.destination.name if trip.destination else None,
                "business_miles_before_trip": business_miles_before_trip[trip.id],
                "rate_schedule": schedule,
                **calculation,
            }
        )
    return {
        "mileage_band_policy": "per_volunteer_tax_year",
        "total_amount_pence": sum(item["total_amount_pence"] for item in breakdowns),
        "trips": breakdowns,
    }


def quote_trip_payment(person_id: int, trip_ids: Iterable[int]) -> dict[str, Any]:
    """Quote selected stored trips for one payee using the same calculation as payment."""
    from .database import unit_of_work

    with unit_of_work() as session:
        return _payment_trip_quote(session, person_id, trip_ids)


def quote_trip_expenses(person_id: int, trip_ids: Iterable[int]) -> dict[str, Any]:
    """Calculate reportable expenses for paid or unpaid stored trips.

    This read-only projection deliberately shares the payment calculator so a
    report cannot drift from the annual mileage-band rules used at payment.
    """
    from .database import unit_of_work

    with unit_of_work() as session:
        return _payment_trip_quote(
            session,
            person_id,
            trip_ids,
            require_unpaid=False,
        )


def get_payment_calculation(payment_id: int) -> dict[str, Any] | None:
    """Read payment evidence on demand without loading it on every list call."""
    from .database import unit_of_work
    from .models import Payment

    with unit_of_work() as session:
        payment = session.get(Payment, payment_id)
        if payment is None:
            return None
        return {
            "payment_id": payment_id,
            "calculation": (
                json.loads(payment.calculation_snapshot) if payment.calculation_snapshot else None
            ),
        }


def create_payment(
    *,
    person_id: int,
    amount_pence: int,
    paid_date: str | None = None,
    method: str = "bank transfer",
    reference: str | None = None,
    batch_id: int | None = None,
    trip_ids: Iterable[int] = (),
    request_token: str | None = None,
) -> dict[str, Any]:
    """Create a payment and optionally attach a set of fully reimbursed trips.

    All trips must belong to the payee, be unpaid, and sum to the payment amount.
    The payment and join rows are committed together so links are all-or-nothing.
    """
    selected_trip_ids = [int(trip_id) for trip_id in trip_ids]
    from sqlalchemy import select
    from .database import unit_of_work
    from .models import Payment, PaymentTrip
    from .repositories import (
        SQLAlchemyPaymentRepository,
        SQLAlchemyPaymentTripRepository,
        SQLAlchemyPersonRepository,
    )

    with unit_of_work() as session:
        if request_token:
            existing = session.scalar(select(Payment).where(Payment.request_token == request_token))
            if existing is not None:
                session.expire(existing, ["trip_links", "donation_links"])
                return _payment_model_to_dict(existing)
        if amount_pence <= 0:
            raise ValueError("Payment amount must be greater than zero.")
        if SQLAlchemyPersonRepository(session).get(person_id) is None:
            raise ValueError("Person was not found.")
        calculation = (
            _payment_trip_quote(session, person_id, selected_trip_ids)
            if selected_trip_ids
            else None
        )
        if calculation is not None and amount_pence != calculation["total_amount_pence"]:
            raise ValueError(
                "Payment amount must equal the total reimbursement for the linked trips "
                f"({calculation['total_amount_pence']} pence)."
            )
        model = SQLAlchemyPaymentRepository(session).add(
            Payment(
                person_id=person_id,
                batch_id=str(batch_id) if batch_id is not None else None,
                amount_pence=str(amount_pence),
                paid_date=paid_date,
                method=method,
                reference=reference,
                calculation_snapshot=json.dumps(calculation) if calculation is not None else None,
                request_token=request_token,
            )
        )
        SQLAlchemyPaymentTripRepository(session).add_all(
            [PaymentTrip(payment_id=model.id, trip_id=trip_id) for trip_id in selected_trip_ids]
        )
        session.expire(model, ["trip_links", "donation_links"])
        return _payment_model_to_dict(model)


def _payment_model_to_dict(model) -> dict[str, Any]:
    """Serialize a payment with linked trip details and active donation totals."""
    linked_trips = sorted(
        (link.trip for link in model.trip_links),
        key=lambda trip: (trip.trip_date, trip.id),
    )
    trip_items = [
        {
            "id": trip.id,
            "trip_date": trip.trip_date,
            "total_miles": float(trip.total_miles),
            "destination_name": trip.destination.name if trip.destination else None,
        }
        for trip in linked_trips
    ]
    active_donations = [link.donation for link in model.donation_links if link.donation.active]
    related_total = sum(int(donation.amount_pence) for donation in active_donations)
    return {
        "id": model.id,
        "person_id": model.person_id,
        "batch_id": int(model.batch_id) if model.batch_id not in (None, "") else None,
        "amount_pence": int(model.amount_pence),
        "paid_date": model.paid_date,
        "method": model.method,
        "reference": model.reference,
        "related_donation_amount_pence": related_total,
        "linked_donation_count": len(active_donations),
        "linked_trips": trip_items,
        "trip_ids": [trip["id"] for trip in trip_items],
        "donated_back_pence": related_total,
    }


def list_payments(
    person_id: int | None = None,
    *,
    limit: int | None = None,
    offset: int = 0,
    with_total: bool = False,
    search: str = "",
) -> list[dict[str, Any]] | tuple[list[dict[str, Any]], int]:
    """List payments with active linked-donation totals and linked-trip details."""
    from sqlalchemy import func, select
    from sqlalchemy.orm import selectinload
    from .database import unit_of_work
    from .models import DonationPayment, Payment, PaymentTrip, Trip

    with unit_of_work() as session:
        filters = () if person_id is None else (Payment.person_id == person_id,)
        total = int(session.scalar(select(func.count()).select_from(Payment).where(*filters)) or 0)
        statement = (
            select(Payment)
            .where(*filters)
            .options(
                selectinload(Payment.donation_links).selectinload(DonationPayment.donation),
                selectinload(Payment.trip_links)
                .selectinload(PaymentTrip.trip)
                .selectinload(Trip.destination),
            )
            .order_by(Payment.paid_date.desc(), Payment.id.desc())
        )
        if limit is not None and not search:
            statement = statement.limit(limit).offset(offset)
        payments = [_payment_model_to_dict(model) for model in session.scalars(statement)]
        if search:
            query = search.casefold()
            payments = [
                item for item in payments if query in json.dumps(item, default=str).casefold()
            ]
            total = len(payments)
            if limit is not None:
                payments = payments[offset : offset + limit]
        return (payments, total) if with_total else payments


def list_payment_reconciliation() -> list[dict[str, Any]]:
    """Aggregate payments and donations linked to payments by person."""
    from .database import unit_of_work
    from .repositories import SQLAlchemyReportRepository

    with unit_of_work() as session:
        return SQLAlchemyReportRepository(session).payment_reconciliation()


def get_summary() -> dict[str, Any]:
    """Return dashboard counts and active donation/payment totals in pence."""
    from .database import unit_of_work
    from .repositories import SQLAlchemyReportRepository

    with unit_of_work() as session:
        return SQLAlchemyReportRepository(session).summary()


def _rate_schedule_for_session(session, effective_date: date | str) -> dict[str, Any]:
    """Load the last schedule effective on or before a trip date through the ORM."""
    from sqlalchemy import select
    from .models import MileageRateSchedule

    date_value = effective_date.isoformat() if isinstance(effective_date, date) else effective_date
    model = session.scalar(
        select(MileageRateSchedule)
        .where(
            MileageRateSchedule.effective_from <= date_value,
        )
        .order_by(MileageRateSchedule.effective_from.desc())
        .limit(1)
    )
    if model is None:
        raise ValueError(f"No mileage rate schedule applies on {date_value}.")
    return _rate_model_to_dict(model)


def _rate_model_to_dict(model) -> dict[str, Any]:
    """Serialize one mileage-rate ORM model without derived period end dates."""
    return {
        "effective_from": model.effective_from,
        "car_first_10000_pence": model.car_first_10000_pence,
        "car_over_10000_pence": model.car_over_10000_pence,
        "motorcycle_pence": model.motorcycle_pence,
        "bicycle_pence": model.bicycle_pence,
        "passenger_pence": model.passenger_pence,
        "created_at": model.created_at,
    }


def get_rate_schedule_for_date(effective_date: date | str) -> dict[str, Any]:
    """Return the complete rate schedule that applies to a given trip date."""
    from .database import unit_of_work

    with unit_of_work() as session:
        return _rate_schedule_for_session(session, effective_date)


def list_rate_schedules() -> list[dict[str, Any]]:
    """List complete schedules with inclusive end dates derived from the next one."""
    from sqlalchemy import select
    from .database import unit_of_work
    from .models import MileageRateSchedule

    with unit_of_work() as session:
        schedules = [
            _rate_model_to_dict(model)
            for model in session.scalars(
                select(MileageRateSchedule).order_by(MileageRateSchedule.effective_from)
            )
        ]
    for index, schedule in enumerate(schedules):
        next_start = schedules[index + 1]["effective_from"] if index + 1 < len(schedules) else None
        # Store only start dates; derive inclusive end dates to avoid editing old schedules.
        schedule["effective_to"] = (
            (date.fromisoformat(next_start) - timedelta(days=1)).isoformat() if next_start else None
        )
    return schedules


def create_rate_schedule(
    *,
    effective_from: str,
    car_first_10000_pence: int,
    car_over_10000_pence: int,
    motorcycle_pence: int,
    bicycle_pence: int,
    passenger_pence: int,
) -> dict[str, Any]:
    """Add an immutable complete schedule effective from the supplied date."""
    try:
        parsed_date = date.fromisoformat(effective_from)
    except (TypeError, ValueError) as exc:
        raise ValueError("Effective date must be a valid YYYY-MM-DD date.") from exc
    # The reserved year-one date anchors the original built-in rates permanently.
    if parsed_date.isoformat() != effective_from or parsed_date == date.min:
        raise ValueError("Choose a valid effective date after the original baseline.")

    rate_values = {
        "car_first_10000_pence": car_first_10000_pence,
        "car_over_10000_pence": car_over_10000_pence,
        "motorcycle_pence": motorcycle_pence,
        "bicycle_pence": bicycle_pence,
        "passenger_pence": passenger_pence,
    }
    # Reject bools and floats even though Python considers bool an int subtype.
    for field, value in rate_values.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(
                f"{field.replace('_', ' ')} must be a whole non-negative pence amount."
            )
        if value > 2**63 - 1:
            raise ValueError(f"{field.replace('_', ' ')} is too large.")

    from .database import unit_of_work
    from .models import MileageRateSchedule
    from .repositories import SQLAlchemyMileageRateRepository

    with unit_of_work() as session:
        repository = SQLAlchemyMileageRateRepository(session)
        if repository.get(parsed_date.isoformat()) is not None:
            # Preserve the compatibility exception handled by the existing Flask view.
            raise sqlite3.IntegrityError("A mileage rate schedule already starts on this date.")
        model = repository.add(
            MileageRateSchedule(
                effective_from=parsed_date.isoformat(),
                **rate_values,
            )
        )
        return _rate_model_to_dict(model)


def get_dashboard_checks() -> dict[str, int | bool]:
    """Return staff-facing record checks without inferring approval or payment status."""
    from .database import unit_of_work
    from .repositories import SQLAlchemyReportRepository

    with unit_of_work() as session:
        return SQLAlchemyReportRepository(session).dashboard_checks()


def get_person_dashboard(person_id: int | None) -> dict[str, Any]:
    """Return a volunteer's trip count and recent trips, scoped to their person ID."""
    from .database import unit_of_work
    from .repositories import SQLAlchemyReportRepository

    with unit_of_work() as session:
        return SQLAlchemyReportRepository(session).person_dashboard(person_id)


def get_financial_report() -> dict[str, Any]:
    """Return financial aggregates, including currently outstanding expenses.

    Outstanding mileage is calculated in one chronological pass across all
    trips.  Annual car-mile totals are accumulated per volunteer and tax year,
    preserving the same band rules as payment quotation without issuing trip
    and rate-schedule queries once per volunteer.

    No rows are changed here: the SQLAlchemy session is used only for reads and
    the calculated pence values are added to the response dictionaries.
    """
    from .database import unit_of_work
    from .repositories import SQLAlchemyReportRepository

    with unit_of_work() as session:
        report = SQLAlchemyReportRepository(session).financial_report()
        outstanding = _outstanding_expenses_by_person(session)
        for volunteer in report["volunteers"]:
            volunteer["expenses_outstanding_pence"] = outstanding.get(volunteer["person_id"], 0)
        report["totals"]["expenses_outstanding_pence"] = sum(outstanding.values())
        return report


def _outstanding_expenses_by_person(session) -> dict[int, int]:
    """Price every unpaid trip using batched data and annual mileage totals."""
    from bisect import bisect_right

    from sqlalchemy import select

    from .models import MileageRateSchedule, PaymentTrip, Trip

    schedules = [
        _rate_model_to_dict(model)
        for model in session.scalars(
            select(MileageRateSchedule).order_by(MileageRateSchedule.effective_from)
        )
    ]
    schedule_dates = [rate["effective_from"] for rate in schedules]
    paid_trip_ids = set(session.scalars(select(PaymentTrip.trip_id)))
    trips = session.scalars(select(Trip).order_by(Trip.person_id, Trip.trip_date, Trip.id))

    annual_car_miles: dict[tuple[int, str], float] = {}
    outstanding: dict[int, int] = {}
    for trip in trips:
        tax_year = trip.tax_year or calculate_tax_year(date.fromisoformat(trip.trip_date))
        annual_key = (trip.person_id, tax_year)
        miles_before = annual_car_miles.get(annual_key, 0.0)
        if trip.id not in paid_trip_ids:
            schedule_index = bisect_right(schedule_dates, trip.trip_date) - 1
            if schedule_index < 0:
                raise ValueError(f"No mileage rate schedule applies on {trip.trip_date}.")
            calculation = calculate_trip_amounts(
                vehicle_type=trip.vehicle_type,
                trip_date=date.fromisoformat(trip.trip_date),
                miles_one_way=float(trip.miles_one_way),
                return_trip=bool(trip.return_trip),
                passengers=int(trip.passengers),
                total_business_miles=miles_before,
                rate_bands=rate_schedule_to_bands(schedules[schedule_index]),
            )
            outstanding[trip.person_id] = (
                outstanding.get(trip.person_id, 0) + calculation["total_amount_pence"]
            )
        if trip.vehicle_type == "car":
            annual_car_miles[annual_key] = miles_before + float(trip.total_miles)
    return outstanding
