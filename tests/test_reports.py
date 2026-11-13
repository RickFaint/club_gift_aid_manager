"""Dashboard and role-scoped reporting behavior.

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

@pytest.mark.parametrize(
    ("role", "expected_text", "unexpected_text"),
    [
        ("admin", b"Admin guide", None),
        ("treasurer", b"User guide", b"Admin guide"),
        ("volunteer", b"User guide", b"Admin guide"),
    ],
)
def test_how_it_works_page_shows_guide_for_role(role, expected_text, unexpected_text):
    """The guide is available to signed-in users and admin details stay private."""
    person = create_person(name=f"{role} guide person") if role == "volunteer" else None
    client = signed_in_client(role=role, person_id=person["id"] if person else None)

    response = client.get("/how-it-works")

    assert response.status_code == 200
    assert expected_text in response.data
    if unexpected_text:
        assert unexpected_text not in response.data



def test_dashboard_overview_returns_staff_record_checks_and_admin_activity():
    """Staff see useful data checks, while recent audit activity remains admin-only."""
    person = create_person(name="Dashboard volunteer")
    create_trip(
        person_id=person["id"],
        vehicle_type="car",
        trip_date=date(2026, 10, 1),
        miles_one_way=10,
        return_trip=False,
        declaration_text="I accept the mileage declaration.",
    )
    create_donation(
        person_id=person["id"],
        amount_pence=1000,
        gift_aid_eligible=True,
    )
    active_declaration = create_declaration(
        person_id=person["id"],
        full_name="Dashboard donor",
    )
    create_donation(
        person_id=person["id"],
        amount_pence=1000,
        declaration_id=active_declaration["id"],
        gift_aid_eligible=True,
    )
    withdrawn_declaration = create_declaration(
        person_id=person["id"],
        full_name="Withdrawn donor",
        withdrawn_date="2026-10-10",
    )
    create_donation(
        person_id=person["id"],
        amount_pence=1000,
        declaration_id=withdrawn_declaration["id"],
        gift_aid_eligible=True,
    )
    dormant_declaration = create_declaration(
        person_id=person["id"],
        full_name="Dormant donor",
    )
    deactivate_declaration(dormant_declaration["id"])
    create_donation(
        person_id=person["id"],
        amount_pence=1000,
        declaration_id=dormant_declaration["id"],
        gift_aid_eligible=True,
    )
    create_donation(
        person_id=person["id"],
        amount_pence=1000,
        gift_aid_eligible=False,
    )
    inactive_donation = create_donation(
        person_id=person["id"],
        amount_pence=1000,
        gift_aid_eligible=True,
    )
    update_donation(inactive_donation["id"], active=False)
    create_destination(name="Dashboard ground", home_ground=True)

    admin_overview = signed_in_client("admin").get("/dashboard/overview")
    treasurer_overview = signed_in_client("treasurer").get("/dashboard/overview")

    assert admin_overview.status_code == 200
    admin_data = admin_overview.get_json()
    assert admin_data["trips_without_payment"] == 1
    assert admin_data["eligible_donations_without_active_declaration"] == 3
    assert admin_data["has_active_home_ground"] is True
    assert "recent_activity" in admin_data

    assert treasurer_overview.status_code == 200
    treasurer_data = treasurer_overview.get_json()
    assert treasurer_data["trips_without_payment"] == 1
    assert "recent_activity" not in treasurer_data

