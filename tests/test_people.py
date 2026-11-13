"""People records, anonymisation, ownership, and DSAR export behavior.

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

def test_admin_person_export_collects_scoped_dsar_records():
    """The export gathers only the selected person's application records."""
    selected = create_person(name="DSAR selected")
    other = create_person(name="DSAR other")
    create_donation(person_id=selected["id"], amount_pence=500)
    create_donation(person_id=other["id"], amount_pence=700)

    response = signed_in_client("admin").get(f"/people/{selected['id']}/export")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["person"]["id"] == selected["id"]
    assert [donation["person_id"] for donation in payload["donations"]] == [selected["id"]]
    assert set(payload) == {"person", "trips", "declarations", "donations", "payments"}



def test_anonymizing_person_preserves_transactions_and_audit_history():
    """Person erasure clears identifiers and linked login but retains record history."""
    person = create_person(
        name="Sensitive Person",
        email="private@example.com",
        phone="555-0123",
        notes="Private note",
    )
    account = create_user(
        username="sensitive-person",
        password="a-secure-test-password",
        role="volunteer",
        person_id=person["id"],
    )
    declaration = create_declaration(
        person_id=person["id"],
        full_name="Sensitive Person",
        home_address="1 Private Street",
        postcode="AB1 2CD",
        evidence_ref="signed-form-123",
    )
    destination = create_destination(name="Retained trip ground")
    trip = create_trip(
        person_id=person["id"],
        origin_id=destination["id"],
        destination_id=destination["id"],
        vehicle_type="car",
        trip_date=date(2026, 8, 1),
        miles_one_way=17.5,
        return_trip=True,
        declaration_text="Required declaration.",
    )
    donation = create_donation(person_id=person["id"], amount_pence=2400)
    payment = create_payment(person_id=person["id"], amount_pence=3500)
    admin = signed_in_client()
    person_client = signed_in_client("volunteer", person["id"])

    response = admin.delete(
        f"/people/{person['id']}",
        headers={"X-CSRF-Token": "test-csrf-token"},
    )

    assert response.status_code == 200
    assert response.get_json()["anonymized"] is True
    anonymized_person = get_person(person["id"])
    assert anonymized_person["name"] == f"Deleted person {person['id']}"
    assert anonymized_person["email"] is None
    assert anonymized_person["phone"] is None
    assert anonymized_person["notes"] is None
    assert anonymized_person["active"] == 0

    retained_declaration = db_module.list_declarations(person["id"])[0]
    assert retained_declaration["id"] == declaration["id"]
    assert retained_declaration["full_name"] == f"Deleted person {person['id']}"
    assert retained_declaration["home_address"] is None
    assert retained_declaration["postcode"] is None
    assert retained_declaration["evidence_ref"] is None

    retained_trip = next(row for row in db_module.list_trips() if row["id"] == trip["id"])
    assert retained_trip["person_id"] == person["id"]
    assert retained_trip["miles_one_way"] == 17.5
    assert retained_trip["total_miles"] == 35.0
    assert next(row for row in list_donations() if row["id"] == donation["id"])["amount_pence"] == 2400
    assert next(row for row in list_payments() if row["id"] == payment["id"])["amount_pence"] == 3500
    assert get_user_by_id(account["id"])["is_active"] == 0
    assert get_user_by_id(account["id"])["person_id"] is None
    assert person_client.get("/trips").status_code == 401
    audit_events = admin.get("/admin/audit").get_json()["items"]
    assert any(
        event["action"] == "anonymized"
        and event["entity_type"] == "person"
        and event["entity_id"] == str(person["id"])
        for event in audit_events
    )

