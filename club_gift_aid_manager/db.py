"""SQLite persistence layer for accounts and club mileage/Gift Aid records.

Each public helper owns a short-lived connection, performs the requested
database operation, commits writes, and closes the connection in ``finally``.
Business rules that protect relationships (for example, linking only a person's
own declaration/payment/trips) are checked here as well as in the HTTP layer.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextvars import ContextVar
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

from werkzeug.security import generate_password_hash

from .services import (
    BASE_RATE_BANDS,
    calculate_tax_year,
    calculate_trip_amounts,
    rate_schedule_to_bands,
    validate_declaration,
)

DB_PATH = Path(os.environ.get(
    "CLUB_GIFT_AID_DB",
    str(Path(__file__).resolve().parent.parent / "club_gift_aid_manager.sqlite3"),
))

_REQUEST_CONNECTION: ContextVar[sqlite3.Connection | None] = ContextVar(
    "club_gift_aid_request_connection",
    default=None,
)


class _RequestConnection(sqlite3.Connection):
    """Keep helper-level commits and closes inside the surrounding request transaction."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.defer_commit = False
        self.defer_close = False

    def commit(self) -> None:
        if not self.defer_commit:
            super().commit()

    def close(self) -> None:
        if not self.defer_close:
            super().close()


def get_connection() -> sqlite3.Connection:
    """Open the configured SQLite database and return rows by column name."""
    request_connection = _REQUEST_CONNECTION.get()
    if request_connection is not None:
        return request_connection

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        descriptor = os.open(DB_PATH, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
    conn = sqlite3.connect(DB_PATH, factory=_RequestConnection)
    conn.row_factory = sqlite3.Row
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
    _REQUEST_CONNECTION.set(conn)


def finish_request_transaction(*, commit: bool) -> None:
    """Commit a successful request or roll back its changes and audit events together."""
    conn = _REQUEST_CONNECTION.get()
    if conn is None:
        return
    _REQUEST_CONNECTION.set(None)
    conn.defer_commit = False
    conn.defer_close = False
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


def create_app_database() -> None:
    """Create missing tables and migrate columns for databases from older app versions.

    The schema is kept in one idempotent script. The following column checks
    add newer fields only when absent, preserving existing stored records.
    """
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
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin', 'treasurer', 'volunteer')),
                person_id INTEGER,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(person_id) REFERENCES people(id)
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
                FOREIGN KEY(person_id) REFERENCES people(id)
            );

            -- Geocodable grounds used as trip origins and destinations.
            CREATE TABLE IF NOT EXISTS destinations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                address TEXT,
                town_city TEXT,
                postcode TEXT,
                notes TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                home_ground INTEGER NOT NULL DEFAULT 0,
                latitude REAL,
                longitude REAL
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
                miles_one_way REAL NOT NULL,
                return_trip INTEGER NOT NULL,
                total_miles REAL NOT NULL,
                passengers INTEGER NOT NULL,
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
                amount_pence INTEGER NOT NULL,
                donated_date TEXT,
                method TEXT,
                reference TEXT,
                declaration_id INTEGER,
                gift_aid_eligible INTEGER NOT NULL DEFAULT 0,
                gift_aid_claimed_date TEXT,
                linked_payment_id INTEGER,
                received_date TEXT,
                bank_reference TEXT,
                receipt_id INTEGER,
                FOREIGN KEY(person_id) REFERENCES people(id),
                FOREIGN KEY(declaration_id) REFERENCES gift_aid_declarations(id)
            );

            -- Money paid to a person as reimbursement for one or more complete trips.
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                person_id INTEGER NOT NULL,
                batch_id INTEGER,
                amount_pence INTEGER NOT NULL,
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

            -- Short-lived, pseudonymous counters throttle repeated login guesses.
            CREATE TABLE IF NOT EXISTS login_attempts (
                attempt_key TEXT PRIMARY KEY,
                failure_count INTEGER NOT NULL,
                window_started_at INTEGER NOT NULL,
                locked_until INTEGER NOT NULL DEFAULT 0
            );
            """
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
        trip_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(trips)").fetchall()
        }
        destination_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(destinations)").fetchall()
        }
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
            row["name"] for row in conn.execute("PRAGMA table_info(gift_aid_declarations)").fetchall()
        }
        # Existing gifts remain included in totals when the dormancy feature is introduced.
        if "active" not in donation_columns:
            conn.execute(
                "ALTER TABLE donations ADD COLUMN active INTEGER NOT NULL DEFAULT 1"
            )
        if "active" not in declaration_columns:
            conn.execute(
                "ALTER TABLE gift_aid_declarations ADD COLUMN active INTEGER NOT NULL DEFAULT 1"
            )
        # Preserve existing single-payment links in the new many-to-many relation.
        conn.execute(
            """INSERT OR IGNORE INTO donation_payments (donation_id, payment_id)
               SELECT id, linked_payment_id FROM donations
               WHERE linked_payment_id IS NOT NULL"""
        )
        # Legacy trip records are preserved and receive nullable location links.
        if "destination_id" not in trip_columns:
            conn.execute("ALTER TABLE trips ADD COLUMN destination_id INTEGER REFERENCES destinations(id)")
        if "origin_id" not in trip_columns:
            conn.execute("ALTER TABLE trips ADD COLUMN origin_id INTEGER REFERENCES destinations(id)")
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
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO audit_events (
                   actor_user_id, actor_username, action, entity_type, entity_id, changed_fields
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                actor_user_id,
                actor_username,
                action,
                entity_type,
                str(entity_id),
                json.dumps(sorted(set(changed_fields))),
            ),
        )
        conn.commit()
    finally:
        conn.close()


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
    placeholders = ",".join("?" for _ in keys)
    conn = get_connection()
    try:
        conn.execute(
            "DELETE FROM login_attempts WHERE locked_until <= ? AND window_started_at < ?",
            (now, now - window_seconds),
        )
        row = conn.execute(
            f"SELECT MAX(locked_until) AS locked_until FROM login_attempts "
            f"WHERE attempt_key IN ({placeholders})",
            keys,
        ).fetchone()
        return max(0, int(row["locked_until"] or 0) - now)
    finally:
        conn.close()


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
    conn = get_connection()
    newly_locked = False
    longest_lock = 0
    try:
        conn.execute(
            "DELETE FROM login_attempts WHERE window_started_at < ? AND locked_until < ?",
            (now - window_seconds - lockout_seconds, now),
        )
        for key in keys:
            row = conn.execute(
                "SELECT failure_count, window_started_at, locked_until "
                "FROM login_attempts WHERE attempt_key = ?",
                (key,),
            ).fetchone()
            if row is None or now - row["window_started_at"] >= window_seconds:
                failure_count = 1
                window_started_at = now
            else:
                failure_count = row["failure_count"] + 1
                window_started_at = row["window_started_at"]
            threshold = (
                max_failures[key] if isinstance(max_failures, dict) else max_failures
            )
            locked_until = now + lockout_seconds if failure_count >= threshold else 0
            newly_locked = newly_locked or (
                locked_until > now and (row is None or row["locked_until"] <= now)
            )
            conn.execute(
                """INSERT INTO login_attempts (
                       attempt_key, failure_count, window_started_at, locked_until
                   ) VALUES (?, ?, ?, ?)
                   ON CONFLICT(attempt_key) DO UPDATE SET
                       failure_count = excluded.failure_count,
                       window_started_at = excluded.window_started_at,
                       locked_until = excluded.locked_until""",
                (key, failure_count, window_started_at, locked_until),
            )
            longest_lock = max(longest_lock, locked_until - now)
        conn.commit()
        return newly_locked, max(0, longest_lock)
    finally:
        conn.close()


def clear_login_failures(attempt_keys: Iterable[str]) -> None:
    """Clear the successful user's and source address's transient failure counters."""
    keys = tuple(set(attempt_keys))
    if not keys:
        return
    placeholders = ",".join("?" for _ in keys)
    conn = get_connection()
    try:
        conn.execute(
            f"DELETE FROM login_attempts WHERE attempt_key IN ({placeholders})",
            keys,
        )
        conn.commit()
    finally:
        conn.close()


def list_audit_events(limit: int = 500) -> list[dict[str, Any]]:
    """Return newest audit events, bounded to prevent an unbounded page response."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT id, actor_user_id, actor_username, action, entity_type,
                      entity_id, changed_fields, occurred_at
               FROM audit_events
               ORDER BY id DESC
               LIMIT ?""",
            (max(1, min(int(limit), 1000)),),
        ).fetchall()
        events = [dict(row) for row in rows]
        for event in events:
            event["changed_fields"] = json.loads(event["changed_fields"])
        return events
    finally:
        conn.close()


def create_user(*, username: str, password: str, role: str, person_id: int | None = None) -> dict[str, Any]:
    """Validate a login identity, hash its password, and persist the account."""
    if not username.strip():
        raise ValueError("Username is required.")
    if role not in {"admin", "treasurer", "volunteer"}:
        raise ValueError("Role must be admin, treasurer, or volunteer.")
    if not 12 <= len(password) <= 1024:
        raise ValueError("Password must be between 12 and 1024 characters.")
    if role == "volunteer" and person_id is None:
        raise ValueError("Volunteer accounts must be linked to a person.")
    if role == "admin" and person_id is not None:
        raise ValueError("Administrator accounts cannot be linked to a person.")
    conn = get_connection()
    try:
        if person_id is not None and conn.execute(
            "SELECT 1 FROM people WHERE id = ?", (person_id,)
        ).fetchone() is None:
            raise ValueError("Linked person was not found.")
        cursor = conn.execute(
            """INSERT INTO users (username, password_hash, role, person_id)
               VALUES (?, ?, ?, ?)""",
            (username.strip(), generate_password_hash(password), role, person_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id, username, role, person_id, is_active, created_at FROM users WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def get_user_by_username(username: str) -> dict[str, Any] | None:
    """Find an account case-insensitively; includes its hash for login checking."""
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def get_user_by_id(user_id: int) -> dict[str, Any] | None:
    """Load a session account without exposing its password hash."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, username, role, person_id, is_active, created_at FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def list_users() -> list[dict[str, Any]]:
    """List account metadata for the admin account-management view."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT id, username, role, person_id, is_active, created_at
               FROM users ORDER BY username"""
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def update_user(
    user_id: int,
    *,
    role: str | None = None,
    person_id: int | None = None,
    is_active: bool | None = None,
) -> dict[str, Any] | None:
    """Change role/link/activation while protecting the last active administrator."""
    user = get_user_by_id(user_id)
    if user is None:
        return None
    next_role = role or user["role"]
    next_person_id = person_id if person_id is not None else user["person_id"]
    if next_role == "admin":
        next_person_id = None
    if next_role not in {"admin", "treasurer", "volunteer"}:
        raise ValueError("Role must be admin, treasurer, or volunteer.")
    if next_role == "volunteer" and next_person_id is None:
        raise ValueError("Cannot assign volunteer role without a linked person.")
    assignments = ["role = ?", "person_id = ?"]
    values: list[Any] = [next_role, next_person_id]
    if is_active is not None:
        assignments.append("is_active = ?")
        values.append(int(is_active))
    values.append(user_id)
    conn = get_connection()
    try:
        if next_person_id is not None and conn.execute(
            "SELECT 1 FROM people WHERE id = ?", (next_person_id,)
        ).fetchone() is None:
            raise ValueError("Linked person was not found.")
        if user["role"] == "admin" and (next_role != "admin" or is_active is False):
            # Never allow an operation to remove the final active administrator.
            active_admins = conn.execute(
                "SELECT COUNT(*) AS count FROM users WHERE role = 'admin' AND is_active = 1"
            ).fetchone()["count"]
            if active_admins <= 1:
                raise ValueError("Cannot demote or deactivate the last active administrator.")
        conn.execute(f"UPDATE users SET {', '.join(assignments)} WHERE id = ?", values)
        conn.commit()
    finally:
        conn.close()
    return get_user_by_id(user_id)


def create_person(*, name: str, email: str | None = None, phone: str | None = None, role: str = "volunteer", active: bool = True, notes: str | None = None) -> dict[str, Any]:
    """Insert a contact and return the persisted record."""
    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO people (name, email, phone, role, active, notes) VALUES (?, ?, ?, ?, ?, ?)",
            (name, email, phone, role, int(active), notes),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM people WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def list_people() -> list[dict[str, Any]]:
    """Return all contacts for staff and the selector controls."""
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM people ORDER BY id ASC").fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_person(person_id: int) -> dict[str, Any] | None:
    """Look up one person by primary key."""
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def update_person(person_id: int, **updates: Any) -> dict[str, Any] | None:
    """Apply only recognized person fields and return the updated record."""
    if not updates:
        return get_person(person_id)

    allowed = {"name", "email", "phone", "role", "active", "notes"}
    updates_to_apply = {key: value for key, value in updates.items() if key in allowed}
    if not updates_to_apply:
        return get_person(person_id)

    assignments = ", ".join(f"{key} = ?" for key in updates_to_apply)
    values = [updates_to_apply[key] for key in updates_to_apply]
    conn = get_connection()
    try:
        conn.execute(f"UPDATE people SET {assignments} WHERE id = ?", (*values, person_id))
        conn.commit()
        row = conn.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def anonymize_person(person_id: int) -> bool:
    """Remove direct identifiers while preserving linked trip and financial history."""
    conn = get_connection()
    try:
        person = conn.execute(
            "SELECT id FROM people WHERE id = ?", (person_id,)
        ).fetchone()
        if person is None:
            return False
        conn.execute(
            """UPDATE people
               SET name = ?, email = NULL, phone = NULL, notes = NULL,
                   active = 0, role = 'other'
               WHERE id = ?""",
            (f"Deleted person {person_id}", person_id),
        )
        conn.execute(
            """UPDATE gift_aid_declarations
               SET full_name = ?, home_address = NULL, postcode = NULL, evidence_ref = NULL
               WHERE person_id = ?""",
            (f"Deleted person {person_id}", person_id),
        )
        conn.execute(
            "UPDATE users SET is_active = 0, person_id = NULL WHERE person_id = ?",
            (person_id,),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def create_declaration(*, person_id: int, full_name: str, home_address: str | None = None, postcode: str | None = None, declaration_date: str | None = None, scope: str = "future", withdrawn_date: str | None = None, evidence_ref: str | None = None) -> dict[str, Any]:
    """Store Gift Aid declaration details for the selected donor."""
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO gift_aid_declarations (
                person_id, full_name, home_address, postcode, declaration_date,
                scope, withdrawn_date, evidence_ref
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (person_id, full_name, home_address, postcode, declaration_date, scope, withdrawn_date, evidence_ref),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM gift_aid_declarations WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def get_declaration(declaration_id: int) -> dict[str, Any] | None:
    """Fetch one declaration, including logically deleted records."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM gift_aid_declarations WHERE id = ?", (declaration_id,)
        ).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def update_declaration(declaration_id: int, **fields: Any) -> dict[str, Any] | None:
    """Update declaration details and its logical-active flag."""
    allowed_fields = {
        "person_id", "full_name", "home_address", "postcode", "declaration_date",
        "scope", "withdrawn_date", "evidence_ref", "active",
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

    conn = get_connection()
    try:
        if not conn.execute(
            "SELECT 1 FROM gift_aid_declarations WHERE id = ?", (declaration_id,)
        ).fetchone():
            return None
        if updates:
            assignments = ", ".join(f"{key} = ?" for key in updates)
            conn.execute(
                f"UPDATE gift_aid_declarations SET {assignments} WHERE id = ?",
                (*updates.values(), declaration_id),
            )
            conn.commit()
        row = conn.execute(
            "SELECT * FROM gift_aid_declarations WHERE id = ?", (declaration_id,)
        ).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def deactivate_declaration(declaration_id: int) -> bool:
    """Logically delete a declaration without removing evidence or donation links."""
    conn = get_connection()
    try:
        cursor = conn.execute(
            "UPDATE gift_aid_declarations SET active = 0 WHERE id = ?",
            (declaration_id,),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def list_declarations(person_id: int | None = None) -> list[dict[str, Any]]:
    """List declarations, optionally scoped to one person."""
    conn = get_connection()
    try:
        if person_id is None:
            rows = conn.execute("SELECT * FROM gift_aid_declarations ORDER BY id ASC").fetchall()
        else:
            rows = conn.execute("SELECT * FROM gift_aid_declarations WHERE person_id = ? ORDER BY id ASC", (person_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


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
    conn = get_connection()
    try:
        if home_ground:
            conn.execute("UPDATE destinations SET home_ground = 0 WHERE home_ground = 1")
        cursor = conn.execute(
            """INSERT INTO destinations (name, address, town_city, postcode, notes, home_ground)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (cleaned_name, address, town_city, postcode, notes, int(home_ground)),
        )
        conn.commit()
        return dict(conn.execute(
            "SELECT * FROM destinations WHERE id = ?", (cursor.lastrowid,)
        ).fetchone())
    finally:
        conn.close()


def create_destinations(locations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Create a validated set of locations atomically."""
    conn = get_connection()
    request_scoped = _REQUEST_CONNECTION.get() is conn
    try:
        if not request_scoped:
            conn.execute("BEGIN IMMEDIATE")
        existing_conflicts = []
        seen_names = set()
        ascii_lower = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
        for location in locations:
            name = location["name"]
            existing = conn.execute(
                "SELECT name FROM destinations WHERE name = ? COLLATE NOCASE",
                (name,),
            ).fetchone()
            name_key = name.translate(ascii_lower)
            if existing:
                conflict_name = existing["name"]
            elif name_key in seen_names:
                conflict_name = name
            else:
                seen_names.add(name_key)
                continue
            if conflict_name not in existing_conflicts:
                existing_conflicts.append(conflict_name)
        if existing_conflicts:
            conflicts = "; ".join(f'"{name}"' for name in existing_conflicts)
            raise ValueError(f"Location name conflict(s): {conflicts}.")

        if any(location.get("home_ground") for location in locations):
            conn.execute("UPDATE destinations SET home_ground = 0 WHERE home_ground = 1")
        created = []
        for location in locations:
            cursor = conn.execute(
                """INSERT INTO destinations (name, address, town_city, postcode, notes, home_ground)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    location["name"],
                    location.get("address"),
                    location.get("town_city"),
                    location.get("postcode"),
                    location.get("notes"),
                    int(bool(location.get("home_ground"))),
                ),
            )
            created.append(dict(conn.execute(
                "SELECT * FROM destinations WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()))
        conn.commit()
        return created
    finally:
        conn.close()


def list_destinations(*, active_only: bool = False) -> list[dict[str, Any]]:
    """List locations; volunteers receive active locations only for trip choices."""
    conn = get_connection()
    try:
        query = "SELECT * FROM destinations"
        if active_only:
            query += " WHERE active = 1"
        rows = conn.execute(query + " ORDER BY name COLLATE NOCASE").fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_destination(destination_id: int) -> dict[str, Any] | None:
    """Retrieve a location, including cached coordinates used by routing."""
    conn = get_connection()
    try:
        return _row_to_dict(conn.execute(
            "SELECT * FROM destinations WHERE id = ?", (destination_id,)
        ).fetchone())
    finally:
        conn.close()


def save_destination_coordinates(
    destination_id: int, latitude: float, longitude: float
) -> None:
    """Persist geocoded coordinates so later route requests can skip geocoding."""
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE destinations SET latitude = ?, longitude = ? WHERE id = ?",
            (latitude, longitude, destination_id),
        )
        conn.commit()
    finally:
        conn.close()


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
    assignments = ", ".join(f"{key} = ?" for key in updates_to_apply)
    values = [updates_to_apply[key] for key in updates_to_apply]
    conn = get_connection()
    try:
        current = conn.execute(
            "SELECT active FROM destinations WHERE id = ?", (destination_id,)
        ).fetchone()
        if current is None:
            return None
        if updates_to_apply.get("home_ground") and not updates_to_apply.get("active", current["active"]):
            raise ValueError("An inactive location cannot be the club home ground.")
        if updates_to_apply.get("home_ground"):
            # Clear the prior flag before setting this location's flag.
            conn.execute("UPDATE destinations SET home_ground = 0 WHERE home_ground = 1")
        conn.execute(
            f"UPDATE destinations SET {assignments} WHERE id = ?",
            (*values, destination_id),
        )
        if {"name", "address", "town_city", "postcode"} & updates_to_apply.keys():
            # A changed query address invalidates any old geocoded point.
            conn.execute(
                "UPDATE destinations SET latitude = NULL, longitude = NULL WHERE id = ?",
                (destination_id,),
            )
        conn.commit()
        return _row_to_dict(conn.execute(
            "SELECT * FROM destinations WHERE id = ?", (destination_id,)
        ).fetchone())
    finally:
        conn.close()


def delete_destination(destination_id: int) -> bool:
    """Delete an unused location; historical trip references require deactivation."""
    conn = get_connection()
    try:
        if conn.execute(
            "SELECT 1 FROM trips WHERE destination_id = ? OR origin_id = ? LIMIT 1",
            (destination_id, destination_id),
        ).fetchone():
            raise ValueError("Location is used by trip records; deactivate it instead.")
        cursor = conn.execute("DELETE FROM destinations WHERE id = ?", (destination_id,))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


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
) -> dict[str, Any]:
    """Validate and insert a trip, computing its round-trip mileage and tax year."""
    return create_trips(
        [{
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
        }],
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
        prepared.append({
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
        })

    conn = get_connection()
    try:
        created = []
        for trip in prepared:
            if trip["destination_id"] is not None and conn.execute(
                "SELECT 1 FROM destinations WHERE id = ? AND active = 1",
                (trip["destination_id"],),
            ).fetchone() is None:
                raise ValueError("Destination was not found or is inactive.")
            if trip["origin_id"] is not None and conn.execute(
                "SELECT 1 FROM destinations WHERE id = ? AND active = 1",
                (trip["origin_id"],),
            ).fetchone() is None:
                raise ValueError("Origin location was not found or is inactive.")
            cursor = conn.execute(
                """
                INSERT INTO trips (
                    person_id, origin_id, destination_id, vehicle_type, trip_date, tax_year,
                    miles_one_way, return_trip, total_miles, passengers,
                    declaration_accepted, declaration_text, declared_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)
                """,
                (
                    trip["person_id"],
                    trip["origin_id"],
                    trip["destination_id"],
                    trip["vehicle_type"],
                    trip["trip_date"].isoformat(),
                    trip["tax_year"],
                    trip["miles_one_way"],
                    int(trip["return_trip"]),
                    trip["total_miles"],
                    trip["passengers"],
                    int(trip["declaration_accepted"]),
                    declaration_text or "I declare that I have taken other parents' children on this journey.",
                    trip["status"],
                ),
            )
            created.append(_row_to_dict(
                conn.execute("SELECT * FROM trips WHERE id = ?", (cursor.lastrowid,)).fetchone()
            ))
        conn.commit()
        return created
    finally:
        conn.close()


def list_trips(person_id: int | None = None) -> list[dict[str, Any]]:
    """List trip records with readable endpoints and any linked reimbursement ID."""
    conn = get_connection()
    try:
        query = """SELECT trips.*, destinations.name AS destination_name,
                          origins.name AS origin_name,
                          payment_trips.payment_id AS payment_id
                   FROM trips
                   LEFT JOIN destinations ON destinations.id = trips.destination_id
                   LEFT JOIN destinations AS origins ON origins.id = trips.origin_id
                   LEFT JOIN payment_trips ON payment_trips.trip_id = trips.id"""
        if person_id is None:
            rows = conn.execute(query + " ORDER BY trip_date DESC").fetchall()
        else:
            rows = conn.execute(
                query + " WHERE trips.person_id = ? ORDER BY trip_date DESC", (person_id,)
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_trip(trip_id: int) -> dict[str, Any] | None:
    """Fetch one trip with location names and its linked payment, if any."""
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT trips.*, destinations.name AS destination_name,
                      origins.name AS origin_name,
                      payment_trips.payment_id AS payment_id
               FROM trips
               LEFT JOIN destinations ON destinations.id = trips.destination_id
               LEFT JOIN destinations AS origins ON origins.id = trips.origin_id
               LEFT JOIN payment_trips ON payment_trips.trip_id = trips.id
               WHERE trips.id = ?""",
            (trip_id,),
        ).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def update_trip(trip_id: int, **updates: Any) -> dict[str, Any] | None:
    """Update recognized fields while preserving person ownership of paid trips."""
    if not updates:
        return get_trip(trip_id)

    allowed = {"person_id", "origin_id", "destination_id", "vehicle_type", "trip_date", "tax_year", "miles_one_way", "return_trip", "total_miles", "passengers", "declaration_accepted", "declaration_text", "declared_at", "status"}
    updates_to_apply = {key: value for key, value in updates.items() if key in allowed}
    if "person_id" in updates_to_apply:
        updates_to_apply["person_id"] = int(updates_to_apply["person_id"])
    if not updates_to_apply:
        return get_trip(trip_id)

    assignments = ", ".join(f"{key} = ?" for key in updates_to_apply)
    values = [updates_to_apply[key] for key in updates_to_apply]
    conn = get_connection()
    try:
        if "person_id" in updates_to_apply:
            payment = conn.execute(
                """SELECT payments.person_id
                   FROM payment_trips
                   JOIN payments ON payments.id = payment_trips.payment_id
                   WHERE payment_trips.trip_id = ?""",
                (trip_id,),
            ).fetchone()
            if payment is not None and payment["person_id"] != updates_to_apply["person_id"]:
                raise ValueError("A trip linked to a payment cannot be reassigned to another person.")
        for field in ("origin_id", "destination_id"):
            location_id = updates_to_apply.get(field)
            if location_id is not None and conn.execute(
                "SELECT 1 FROM destinations WHERE id = ? AND active = 1",
                (location_id,),
            ).fetchone() is None:
                raise ValueError(f"{field.replace('_', ' ').capitalize()} was not found or is inactive.")
        conn.execute(f"UPDATE trips SET {assignments} WHERE id = ?", (*values, trip_id))
        conn.commit()
        row = conn.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def delete_trip(trip_id: int) -> bool:
    """Delete an unpaid trip; linked trips are protected as payment evidence."""
    conn = get_connection()
    try:
        if conn.execute(
            "SELECT 1 FROM payment_trips WHERE trip_id = ?", (trip_id,)
        ).fetchone():
            raise ValueError("A trip linked to a payment cannot be deleted.")
        cursor = conn.execute("DELETE FROM trips WHERE id = ?", (trip_id,))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def create_donation(*, person_id: int, amount_pence: int, donated_date: str | None = None, method: str | None = None, reference: str | None = None, declaration_id: int | None = None, gift_aid_eligible: bool = False, gift_aid_claimed_date: str | None = None, linked_payment_id: int | None = None, linked_payment_ids: Iterable[int] | None = None, received_date: str | None = None, bank_reference: str | None = None, receipt_id: int | None = None) -> dict[str, Any]:
    """Create a positive donation and validate its optional same-person links."""
    payment_ids = _normalize_payment_ids(linked_payment_ids, linked_payment_id)
    conn = get_connection()
    try:
        if amount_pence <= 0:
            raise ValueError("Donation amount must be greater than zero.")
        if conn.execute("SELECT 1 FROM people WHERE id = ?", (person_id,)).fetchone() is None:
            raise ValueError("Person was not found.")
        _validate_donation_payment_links(conn, payment_ids, person_id)
        if declaration_id is not None and conn.execute(
            "SELECT 1 FROM gift_aid_declarations WHERE id = ? AND person_id = ?",
            (declaration_id, person_id),
        ).fetchone() is None:
            raise ValueError("Linked declaration must belong to the same person.")
        cursor = conn.execute(
            """
            INSERT INTO donations (
                person_id, amount_pence, donated_date, method, reference,
                declaration_id, gift_aid_eligible, gift_aid_claimed_date,
                linked_payment_id, received_date, bank_reference, receipt_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                person_id, amount_pence, donated_date, method, reference,
                declaration_id, int(gift_aid_eligible), gift_aid_claimed_date,
                payment_ids[0] if payment_ids else None,
                received_date, bank_reference, receipt_id,
            ),
        )
        donation_id = cursor.lastrowid
        conn.executemany(
            "INSERT INTO donation_payments (donation_id, payment_id) VALUES (?, ?)",
            [(donation_id, payment_id) for payment_id in payment_ids],
        )
        conn.commit()
        return _donation_with_payment_ids(conn, donation_id)
    finally:
        conn.close()


def _normalize_payment_ids(
    linked_payment_ids: Iterable[int] | None,
    linked_payment_id: int | None = None,
) -> list[int]:
    """Normalize legacy single and current multiple payment-link inputs."""
    values = linked_payment_ids if linked_payment_ids is not None else (
        [] if linked_payment_id in (None, "") else [linked_payment_id]
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
    conn: sqlite3.Connection,
    payment_ids: list[int],
    person_id: int,
) -> None:
    """Require every related payment to belong to the donation's person."""
    if not payment_ids:
        return
    placeholders = ",".join("?" for _ in payment_ids)
    payments = conn.execute(
        f"SELECT id, person_id FROM payments WHERE id IN ({placeholders})",
        payment_ids,
    ).fetchall()
    if len(payments) != len(payment_ids) or any(
        payment["person_id"] != person_id for payment in payments
    ):
        raise ValueError("Linked reimbursement payments must belong to the same person.")


def _donation_with_payment_ids(
    conn: sqlite3.Connection,
    donation_id: int,
) -> dict[str, Any] | None:
    """Return a donation with ordered payment IDs and legacy single-link alias."""
    donation = _row_to_dict(conn.execute(
        "SELECT * FROM donations WHERE id = ?", (donation_id,)
    ).fetchone())
    if donation is None:
        return None
    payment_ids = [
        row["payment_id"] for row in conn.execute(
            "SELECT payment_id FROM donation_payments WHERE donation_id = ? ORDER BY payment_id",
            (donation_id,),
        ).fetchall()
    ]
    donation["linked_payment_ids"] = payment_ids
    donation["linked_payment_id"] = payment_ids[0] if payment_ids else None
    return donation


def update_donation(donation_id: int, **updates: Any) -> dict[str, Any] | None:
    """Edit donation details or active state, validating links after person changes."""
    allowed = {
        "person_id", "amount_pence", "donated_date", "method", "reference",
        "declaration_id", "gift_aid_eligible", "gift_aid_claimed_date",
        "linked_payment_id", "linked_payment_ids", "received_date", "bank_reference",
        "receipt_id", "active",
    }
    updates_to_apply = {key: value for key, value in updates.items() if key in allowed}
    payment_ids_supplied = "linked_payment_ids" in updates_to_apply or "linked_payment_id" in updates_to_apply
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

    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT person_id FROM donations WHERE id = ?", (donation_id,)
        ).fetchone()
        if existing is None:
            return None
        person_id = updates_to_apply.get("person_id", existing["person_id"])
        if conn.execute(
            "SELECT 1 FROM people WHERE id = ?", (person_id,)
        ).fetchone() is None:
            raise ValueError("Person was not found.")
        existing_payment_ids = [
            row["payment_id"] for row in conn.execute(
                "SELECT payment_id FROM donation_payments WHERE donation_id = ?",
                (donation_id,),
            ).fetchall()
        ]
        payment_ids = existing_payment_ids if payment_ids is None else payment_ids
        _validate_donation_payment_links(conn, payment_ids, person_id)
        declaration_id = updates_to_apply.get(
            "declaration_id",
            conn.execute(
                "SELECT declaration_id FROM donations WHERE id = ?", (donation_id,)
            ).fetchone()["declaration_id"],
        )
        if declaration_id is not None and conn.execute(
            "SELECT 1 FROM gift_aid_declarations WHERE id = ? AND person_id = ?",
            (declaration_id, person_id),
        ).fetchone() is None:
            raise ValueError("Linked declaration must belong to the same person.")
        # Only known column names reach this SQL; values remain parameterized.
        if payment_ids_supplied:
            updates_to_apply["linked_payment_id"] = payment_ids[0] if payment_ids else None
        if updates_to_apply:
            assignments = ", ".join(f"{key} = ?" for key in updates_to_apply)
            values = [updates_to_apply[key] for key in updates_to_apply]
            conn.execute(
                f"UPDATE donations SET {assignments} WHERE id = ?",
                (*values, donation_id),
            )
        if payment_ids_supplied:
            conn.execute("DELETE FROM donation_payments WHERE donation_id = ?", (donation_id,))
            conn.executemany(
                "INSERT INTO donation_payments (donation_id, payment_id) VALUES (?, ?)",
                [(donation_id, payment_id) for payment_id in payment_ids],
            )
        conn.commit()
        return _donation_with_payment_ids(conn, donation_id)
    finally:
        conn.close()


def get_donation(donation_id: int) -> dict[str, Any] | None:
    """Fetch a donation by ID, including dormant entries."""
    conn = get_connection()
    try:
        return _donation_with_payment_ids(conn, donation_id)
    finally:
        conn.close()


def set_donation_active(donation_id: int, active: bool) -> dict[str, Any] | None:
    """Toggle the donation's logical-delete flag without removing the row."""
    return update_donation(donation_id, active=active)


def list_donations(person_id: int | None = None) -> list[dict[str, Any]]:
    """List all donations and attach creator metadata when audit history exists."""
    conn = get_connection()
    try:
        query = """
            SELECT donations.*,
                   users_created.actor_username AS created_by,
                   users_created.occurred_at AS created_at
            FROM donations
            LEFT JOIN audit_events AS users_created
              ON users_created.id = (
                  SELECT MIN(audit_events.id)
                  FROM audit_events
                  WHERE audit_events.entity_type = 'donation'
                    AND audit_events.entity_id = CAST(donations.id AS TEXT)
                    AND audit_events.action = 'created'
              )
        """
        if person_id is None:
            rows = conn.execute(
                query + " ORDER BY donations.donated_date DESC, donations.id DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                query + " WHERE donations.person_id = ? ORDER BY donations.donated_date DESC, donations.id DESC",
                (person_id,),
            ).fetchall()
        donations = [dict(row) for row in rows]
        for donation in donations:
            payment_ids = [
                row["payment_id"] for row in conn.execute(
                    "SELECT payment_id FROM donation_payments WHERE donation_id = ? ORDER BY payment_id",
                    (donation["id"],),
                ).fetchall()
            ]
            donation["linked_payment_ids"] = payment_ids
            donation["linked_payment_id"] = payment_ids[0] if payment_ids else None
        return donations
    finally:
        conn.close()


def create_payment(
    *,
    person_id: int,
    amount_pence: int,
    paid_date: str | None = None,
    method: str = "bank transfer",
    reference: str | None = None,
    batch_id: int | None = None,
    trip_ids: Iterable[int] = (),
) -> dict[str, Any]:
    """Create a payment and optionally attach a set of fully reimbursed trips.

    All trips must belong to the payee, be unpaid, and sum to the payment amount.
    The payment and join rows are committed together so links are all-or-nothing.
    """
    selected_trip_ids = [int(trip_id) for trip_id in trip_ids]
    if len(set(selected_trip_ids)) != len(selected_trip_ids):
        raise ValueError("A trip can only be selected once for a payment.")
    conn = get_connection()
    try:
        if amount_pence <= 0:
            raise ValueError("Payment amount must be greater than zero.")
        if conn.execute("SELECT 1 FROM people WHERE id = ?", (person_id,)).fetchone() is None:
            raise ValueError("Person was not found.")
        if selected_trip_ids:
            # Validate the complete trip set before inserting either payment or links.
            placeholders = ",".join("?" for _ in selected_trip_ids)
            trips = conn.execute(
                f"SELECT * FROM trips WHERE id IN ({placeholders})",
                selected_trip_ids,
            ).fetchall()
            if len(trips) != len(selected_trip_ids) or any(
                trip["person_id"] != person_id for trip in trips
            ):
                raise ValueError("Every linked trip must belong to the same person as the payment.")
            allocated = conn.execute(
                f"""SELECT trip_id FROM payment_trips
                    WHERE trip_id IN ({placeholders})""",
                selected_trip_ids,
            ).fetchall()
            if allocated:
                raise ValueError("A selected trip has already been linked to a payment.")
            # A linked payment represents full reimbursement, never partial allocation.
            expected_amount = sum(
                calculate_trip_amounts(
                    vehicle_type=trip["vehicle_type"],
                    trip_date=date.fromisoformat(trip["trip_date"]),
                    miles_one_way=trip["miles_one_way"],
                    return_trip=bool(trip["return_trip"]),
                    passengers=trip["passengers"],
                    rate_bands=rate_schedule_to_bands(
                        _rate_schedule_for_date(conn, trip["trip_date"])
                    ),
                )["total_amount_pence"]
                for trip in trips
            )
            if amount_pence != expected_amount:
                raise ValueError(
                    "Payment amount must equal the total reimbursement for the linked trips "
                    f"({expected_amount} pence)."
                )
        cursor = conn.execute(
            "INSERT INTO payments (person_id, batch_id, amount_pence, paid_date, method, reference) VALUES (?, ?, ?, ?, ?, ?)",
            (person_id, batch_id, amount_pence, paid_date, method, reference),
        )
        # The payment and all of its trip links share this open transaction.
        conn.executemany(
            "INSERT INTO payment_trips (payment_id, trip_id) VALUES (?, ?)",
            [(cursor.lastrowid, trip_id) for trip_id in selected_trip_ids],
        )
        conn.commit()
        row = conn.execute("SELECT * FROM payments WHERE id = ?", (cursor.lastrowid,)).fetchone()
        payment = _row_to_dict(row)
        payment["trip_ids"] = selected_trip_ids
        return payment
    finally:
        conn.close()


def list_payments(person_id: int | None = None) -> list[dict[str, Any]]:
    """List payments with active linked-donation totals and linked-trip details."""
    conn = get_connection()
    try:
        query = """
            SELECT payments.*,
                   COALESCE(SUM(donations.amount_pence), 0) AS related_donation_amount_pence,
                   COUNT(DISTINCT donations.id) AS linked_donation_count
            FROM payments
            LEFT JOIN donation_payments ON donation_payments.payment_id = payments.id
            LEFT JOIN donations
              ON donations.id = donation_payments.donation_id AND donations.active = 1
        """
        if person_id is None:
            rows = conn.execute(
                query + " GROUP BY payments.id ORDER BY payments.paid_date DESC, payments.id DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                query + " WHERE payments.person_id = ? GROUP BY payments.id ORDER BY payments.paid_date DESC, payments.id DESC",
                (person_id,),
            ).fetchall()
        payments = [dict(row) for row in rows]
        if not payments:
            return payments
        payment_ids = [payment["id"] for payment in payments]
        placeholders = ",".join("?" for _ in payment_ids)
        # Batch-load the join rows to avoid one SQL query per payment row.
        linked_trips = conn.execute(
            f"""SELECT payment_trips.payment_id, trips.id AS trip_id, trips.trip_date,
                       trips.total_miles, destinations.name AS destination_name
                FROM payment_trips
                JOIN trips ON trips.id = payment_trips.trip_id
                LEFT JOIN destinations ON destinations.id = trips.destination_id
                WHERE payment_trips.payment_id IN ({placeholders})
                ORDER BY trips.trip_date, trips.id""",
            payment_ids,
        ).fetchall()
        # Group related trips in memory, then attach them to their parent payment.
        trips_by_payment: dict[int, list[dict[str, Any]]] = {
            payment_id: [] for payment_id in payment_ids
        }
        for trip in linked_trips:
            trips_by_payment[trip["payment_id"]].append({
                "id": trip["trip_id"],
                "trip_date": trip["trip_date"],
                "total_miles": trip["total_miles"],
                "destination_name": trip["destination_name"],
            })
        for payment in payments:
            payment["linked_trips"] = trips_by_payment[payment["id"]]
            payment["trip_ids"] = [trip["id"] for trip in payment["linked_trips"]]
            payment["donated_back_pence"] = payment["related_donation_amount_pence"]
        return payments
    finally:
        conn.close()


def list_payment_reconciliation() -> list[dict[str, Any]]:
    """Aggregate payments and donations linked to payments by person."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT people.id AS person_id,
                   people.name AS person_name,
                   payments.payment_total_pence,
                   payments.payment_count,
                   COALESCE(donations.donation_total_pence, 0) AS linked_donation_total_pence,
                   COALESCE(donations.donation_count, 0) AS linked_donation_count,
                   payments.payment_total_pence
                     - COALESCE(donations.donation_total_pence, 0) AS difference_pence
            FROM (
                SELECT person_id,
                       SUM(amount_pence) AS payment_total_pence,
                       COUNT(*) AS payment_count
                FROM payments
                GROUP BY person_id
            ) AS payments
            JOIN people ON people.id = payments.person_id
            -- Each donation contributes once to person-level reconciliation,
            -- even when it references multiple payments.
            LEFT JOIN (
                SELECT donations.person_id,
                       SUM(donations.amount_pence) AS donation_total_pence,
                       COUNT(*) AS donation_count
                FROM donations
                WHERE donations.active = 1
                  AND EXISTS (
                      SELECT 1 FROM donation_payments
                      WHERE donation_payments.donation_id = donations.id
                  )
                GROUP BY donations.person_id
            ) AS donations ON donations.person_id = payments.person_id
            ORDER BY people.name COLLATE NOCASE
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_summary() -> dict[str, Any]:
    """Return dashboard counts and active donation/payment totals in pence."""
    conn = get_connection()
    try:
        people_count = conn.execute("SELECT COUNT(*) AS count FROM people").fetchone()["count"]
        trips_count = conn.execute("SELECT COUNT(*) AS count FROM trips").fetchone()["count"]
        total_donations = conn.execute("SELECT COALESCE(SUM(amount_pence), 0) AS total FROM donations WHERE active = 1").fetchone()["total"]
        total_payments = conn.execute("SELECT COALESCE(SUM(amount_pence), 0) AS total FROM payments").fetchone()["total"]
        return {
            "people": people_count,
            "trips": trips_count,
            "donations_pence": total_donations,
            "payments_pence": total_payments,
        }
    finally:
        conn.close()


def _rate_schedule_for_date(
    conn: sqlite3.Connection,
    effective_date: date | str,
) -> dict[str, Any]:
    """Fetch the last complete schedule effective on or before a trip date."""
    date_value = effective_date.isoformat() if isinstance(effective_date, date) else effective_date
    # ISO dates sort chronologically, so the latest eligible start is the active schedule.
    row = conn.execute(
        """SELECT * FROM mileage_rate_schedules
           WHERE effective_from <= ?
           ORDER BY effective_from DESC
           LIMIT 1""",
        (date_value,),
    ).fetchone()
    if row is None:
        raise ValueError(f"No mileage rate schedule applies on {date_value}.")
    return dict(row)


def get_rate_schedule_for_date(effective_date: date | str) -> dict[str, Any]:
    """Return the complete rate schedule that applies to a given trip date."""
    conn = get_connection()
    try:
        return _rate_schedule_for_date(conn, effective_date)
    finally:
        conn.close()


def list_rate_schedules() -> list[dict[str, Any]]:
    """List complete schedules with inclusive end dates derived from the next one."""
    conn = get_connection()
    try:
        schedules = [
            dict(row) for row in conn.execute(
                "SELECT * FROM mileage_rate_schedules ORDER BY effective_from"
            ).fetchall()
        ]
    finally:
        conn.close()
    for index, schedule in enumerate(schedules):
        next_start = schedules[index + 1]["effective_from"] if index + 1 < len(schedules) else None
        # Store only start dates; derive inclusive end dates to avoid editing old schedules.
        schedule["effective_to"] = (
            (date.fromisoformat(next_start) - timedelta(days=1)).isoformat()
            if next_start else None
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
            raise ValueError(f"{field.replace('_', ' ')} must be a whole non-negative pence amount.")
        if value > 2**63 - 1:
            raise ValueError(f"{field.replace('_', ' ')} is too large.")

    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO mileage_rate_schedules (
                   effective_from, car_first_10000_pence, car_over_10000_pence,
                   motorcycle_pence, bicycle_pence, passenger_pence
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                parsed_date.isoformat(),
                rate_values["car_first_10000_pence"],
                rate_values["car_over_10000_pence"],
                rate_values["motorcycle_pence"],
                rate_values["bicycle_pence"],
                rate_values["passenger_pence"],
            ),
        )
        conn.commit()
        return dict(conn.execute(
            "SELECT * FROM mileage_rate_schedules WHERE effective_from = ?",
            (parsed_date.isoformat(),),
        ).fetchone())
    finally:
        conn.close()


def get_dashboard_checks() -> dict[str, int | bool]:
    """Return staff-facing record checks without inferring approval or payment status."""
    conn = get_connection()
    try:
        # A missing payment link is only a record check; it does not assert approval.
        unpaid_trip_count = conn.execute(
            """SELECT COUNT(*) AS count
               FROM trips
               LEFT JOIN payment_trips ON payment_trips.trip_id = trips.id
               WHERE payment_trips.trip_id IS NULL"""
        ).fetchone()["count"]
        # Count only donations already flagged eligible but lacking a usable linked declaration.
        missing_declaration_count = conn.execute(
            """SELECT COUNT(*) AS count
               FROM donations
               LEFT JOIN gift_aid_declarations
                 ON gift_aid_declarations.id = donations.declaration_id
               WHERE donations.active = 1
                 AND donations.gift_aid_eligible = 1
                 AND (
                     gift_aid_declarations.id IS NULL
                     OR gift_aid_declarations.active = 0
                     OR gift_aid_declarations.withdrawn_date IS NOT NULL
                 )"""
        ).fetchone()["count"]
        has_active_home_ground = conn.execute(
            """SELECT EXISTS(
                   SELECT 1 FROM destinations
                   WHERE active = 1 AND home_ground = 1
               ) AS found"""
        ).fetchone()["found"]
        return {
            "trips_without_payment": int(unpaid_trip_count),
            "eligible_donations_without_active_declaration": int(missing_declaration_count),
            "has_active_home_ground": bool(has_active_home_ground),
        }
    finally:
        conn.close()


def get_person_dashboard(person_id: int | None) -> dict[str, Any]:
    """Return a volunteer's trip count and recent trips, scoped to their person ID."""
    if person_id is None:
        return {"trip_count": 0, "recent_trips": []}
    conn = get_connection()
    try:
        trip_count = conn.execute(
            "SELECT COUNT(*) AS count FROM trips WHERE person_id = ?",
            (person_id,),
        ).fetchone()["count"]
        recent_trips = conn.execute(
            """SELECT trips.trip_date, trips.status, destinations.name AS destination_name
               FROM trips
               LEFT JOIN destinations ON destinations.id = trips.destination_id
               WHERE trips.person_id = ?
               ORDER BY trips.trip_date DESC, trips.id DESC
               LIMIT 3""",
            (person_id,),
        ).fetchall()
        return {
            "trip_count": int(trip_count),
            "recent_trips": [dict(trip) for trip in recent_trips],
        }
    finally:
        conn.close()


def get_financial_report() -> dict[str, Any]:
    """Return club-wide totals and mileage/payment/donation aggregates by volunteer."""
    conn = get_connection()
    try:
        totals = conn.execute(
            """
            SELECT
                (SELECT COALESCE(SUM(amount_pence), 0) FROM payments) AS expenses_paid_pence,
                (SELECT COALESCE(SUM(amount_pence), 0) FROM donations WHERE active = 1) AS donations_pence,
                (SELECT COALESCE(SUM(amount_pence), 0) FROM donations
                 WHERE active = 1 AND gift_aid_eligible = 1) AS eligible_donations_pence
            """
        ).fetchone()
        volunteers = conn.execute(
            """
            SELECT people.id AS person_id,
                   people.name AS person_name,
                   COALESCE(trips.trip_count, 0) AS trip_count,
                   COALESCE(trips.total_miles, 0) AS total_miles,
                   COALESCE(payments.expenses_paid_pence, 0) AS expenses_paid_pence,
                   COALESCE(donations.donations_pence, 0) AS donations_pence,
                   COALESCE(payments.expenses_paid_pence, 0)
                     - COALESCE(donations.donations_pence, 0) AS difference_pence
            FROM people
            LEFT JOIN (
                SELECT person_id, COUNT(*) AS trip_count, SUM(total_miles) AS total_miles
                FROM trips GROUP BY person_id
            ) AS trips ON trips.person_id = people.id
            LEFT JOIN (
                SELECT person_id, SUM(amount_pence) AS expenses_paid_pence
                FROM payments GROUP BY person_id
            ) AS payments ON payments.person_id = people.id
            LEFT JOIN (
                SELECT person_id, SUM(amount_pence) AS donations_pence
                FROM donations WHERE active = 1 GROUP BY person_id
            ) AS donations ON donations.person_id = people.id
            WHERE people.role = 'volunteer'
            ORDER BY people.name COLLATE NOCASE, people.id
            """
        ).fetchall()
        eligible_donations_pence = int(totals["eligible_donations_pence"])
        return {
            "totals": {
                "expenses_paid_pence": int(totals["expenses_paid_pence"]),
                "donations_pence": int(totals["donations_pence"]),
                "eligible_donations_pence": eligible_donations_pence,
                "gift_aid_pence": (eligible_donations_pence * 25 + 50) // 100,
            },
            "volunteers": [dict(row) for row in volunteers],
        }
    finally:
        conn.close()
