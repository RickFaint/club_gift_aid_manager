"""Schema migrations and backward-compatibility behavior.

Each test receives a fresh temporary SQLite database from ``conftest.py``.
Route tests use real Flask clients and database users so permission checks,
request handling, and persistence are exercised together.
"""

from datetime import date
from io import BytesIO
import hashlib
import re
import sqlite3
from pathlib import Path

import pytest

import app as app_module
import audit_export
import club_gift_aid_manager.db as db_module
import club_gift_aid_manager.routing as routing_module
from app import app
from club_gift_aid_manager.config import get_declaration_text, update_declaration_text
from club_gift_aid_manager.crypto import encrypt_value
from club_gift_aid_manager.routing import RouteLookupError
from club_gift_aid_manager.db import (
    create_app_database,
    create_declaration,
    create_destination,
    create_donation,
    create_payment,
    create_person,
    create_rate_schedule,
    create_trip,
    create_user,
    deactivate_declaration,
    get_person,
    get_connection,
    get_user_by_id,
    get_trip,
    get_rate_schedule_for_date,
    get_summary,
    list_rate_schedules,
    list_audit_events,
    list_destinations,
    list_declarations,
    list_donations,
    list_payment_reconciliation,
    list_payments,
    list_people,
    save_destination_coordinates,
    update_donation,
)
from club_gift_aid_manager.services import calculate_tax_year, calculate_trip_amounts, validate_declaration


from tests.helpers import signed_in_client

def test_existing_single_donation_payment_link_migrates_to_join_table():
    """Database initialization preserves legacy donation-payment references."""
    person = create_person(name="Legacy link donor")
    payment = create_payment(person_id=person["id"], amount_pence=1600)
    donation = create_donation(
        person_id=person["id"],
        amount_pence=500,
        linked_payment_id=payment["id"],
    )
    conn = get_connection()
    conn.execute("DROP TABLE donation_payments")
    conn.commit()
    conn.close()

    create_app_database()

    migrated = db_module.get_donation(donation["id"])
    assert migrated["linked_payment_ids"] == [payment["id"]]
    assert list_payments(person["id"])[0]["linked_donation_count"] == 1



def test_legacy_declarations_migrate_to_active_by_default(tmp_path, monkeypatch):
    """Existing declaration rows stay active when the logical-delete flag is added."""
    legacy_path = tmp_path / "legacy-declarations.sqlite3"
    monkeypatch.setattr(db_module, "DB_PATH", legacy_path)
    conn = get_connection()
    conn.execute(
        """CREATE TABLE gift_aid_declarations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id INTEGER NOT NULL,
            full_name TEXT NOT NULL,
            home_address TEXT,
            postcode TEXT,
            declaration_date TEXT,
            scope TEXT,
            withdrawn_date TEXT,
            evidence_ref TEXT
        )"""
    )
    conn.execute(
        "INSERT INTO gift_aid_declarations (person_id, full_name) VALUES (1, ?)",
        (encrypt_value("Legacy Donor"),),
    )
    conn.commit()
    conn.close()

    create_app_database()

    assert db_module.list_declarations()[0]["active"] == 1



def test_existing_trip_table_migrates_destination_id_column(tmp_path, monkeypatch):
    """Database initialization adds location columns to an older trip table."""
    legacy_path = tmp_path / "legacy.sqlite3"
    monkeypatch.setattr(db_module, "DB_PATH", legacy_path)
    conn = db_module.get_connection()
    conn.execute(
        """CREATE TABLE trips (
            id INTEGER PRIMARY KEY,
            person_id INTEGER NOT NULL,
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
            status TEXT NOT NULL DEFAULT 'submitted'
        )"""
    )
    conn.commit()
    conn.close()
    create_app_database()
    conn = db_module.get_connection()
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(trips)")}
    destination_columns = {row["name"] for row in conn.execute("PRAGMA table_info(destinations)")}
    conn.close()
    assert "destination_id" in columns
    assert "origin_id" in columns
    assert {"home_ground", "latitude", "longitude"} <= destination_columns

