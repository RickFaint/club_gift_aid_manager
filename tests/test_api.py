"""Pagination and cross-domain HTTP response-contract tests.

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
    "path",
    [
        "/people", "/trips", "/destinations", "/declarations",
        "/donations", "/payments", "/admin/audit", "/admin/users",
    ],
)
def test_main_list_endpoints_return_pagination_metadata(path):
    """Every primary collection exposes one consistent pagination envelope."""
    response = signed_in_client().get(path)

    assert response.status_code == 200
    payload = response.get_json()
    assert set(payload) == {"items", "total", "limit", "offset"}
    assert payload["limit"] == 50
    assert payload["offset"] == 0
    assert payload["total"] >= len(payload["items"])



def test_people_endpoint_applies_limit_offset_and_maximum():
    """Paging returns the requested slice and caps oversized page requests."""
    for name in ("First page person", "Second page person", "Third page person"):
        create_person(name=name)
    client = signed_in_client()

    page = client.get("/people?limit=2&offset=1").get_json()
    capped = client.get("/people?limit=999&offset=0").get_json()

    assert [person["name"] for person in page["items"]] == [
        "Second page person", "Third page person",
    ]
    assert page == page | {"total": 3, "limit": 2, "offset": 1}
    assert capped["limit"] == 200



def test_paginated_volunteer_lists_keep_role_filters():
    """Totals and pages include only records the volunteer is allowed to see."""
    own_person = create_person(name="Pagination owner")
    other_person = create_person(name="Pagination other")
    create_destination(name="Active pagination location")
    inactive = create_destination(name="Inactive pagination location")
    db_module.update_destination(inactive["id"], active=False)
    for person in (own_person, other_person):
        create_trip(
            person_id=person["id"],
            vehicle_type="car",
            trip_date=date(2026, 11, 1),
            miles_one_way=5,
            return_trip=False,
            declaration_text="I accept.",
        )
    client = signed_in_client("volunteer", own_person["id"])

    trips = client.get("/trips?limit=1").get_json()
    destinations = client.get("/destinations?limit=1").get_json()

    assert trips["total"] == 1
    assert trips["items"][0]["person_id"] == own_person["id"]
    assert destinations["total"] == 1
    assert destinations["items"][0]["active"] == 1

