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
from sqlalchemy import event

import app as app_module
import audit_export
from club_gift_aid_manager import database
import club_gift_aid_manager.db as db_module
import club_gift_aid_manager.services.routing as routing_module
from app import app
from club_gift_aid_manager.core.config import get_declaration_text, update_declaration_text
from club_gift_aid_manager.security.crypto import encrypt_value
from club_gift_aid_manager.services.routing import RouteLookupError
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
    list_users,
    save_destination_coordinates,
    update_donation,
)
from club_gift_aid_manager.services import (
    calculate_tax_year,
    calculate_trip_amounts,
    validate_declaration,
)


from tests.helpers import signed_in_client


@pytest.mark.parametrize(
    "path",
    [
        "/people",
        "/trips",
        "/destinations",
        "/declarations",
        "/donations",
        "/payments",
        "/admin/audit",
        "/admin/users",
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
        "Second page person",
        "Third page person",
    ]
    assert page == page | {"total": 3, "limit": 2, "offset": 1}
    assert capped["limit"] == 200


def test_account_paging_decrypts_only_the_requested_database_page(monkeypatch):
    """Unfiltered account pages do not load every encrypted username before slicing."""
    for username in ("Zulu account", "Alpha account", "Mike account"):
        create_user(
            username=username,
            password="Browser-Page-Test3!",
            role="treasurer",
        )
    from club_gift_aid_manager.models import types as model_types

    decryptions = 0
    original_decrypt = model_types.decrypt_value

    def counting_decrypt(value):
        """Count decrypted fields while retaining the production conversion."""
        nonlocal decryptions
        if value is not None:
            decryptions += 1
        return original_decrypt(value)

    monkeypatch.setattr(model_types, "decrypt_value", counting_decrypt)
    statements = []
    engine = database.get_engine()

    def count_statement(*args):
        """Capture SQL emitted by the bounded page query."""
        statements.append(args[2])

    event.listen(engine, "before_cursor_execute", count_statement)
    try:
        users, total = list_users(limit=1, offset=1, with_total=True)
    finally:
        event.remove(engine, "before_cursor_execute", count_statement)

    assert total == 3
    assert [user["username"] for user in users] == ["Alpha account"]
    assert set(users[0]) == {
        "id",
        "username",
        "role",
        "person_id",
        "is_active",
        "must_change_password",
        "mfa_enabled",
    }
    assert decryptions == 1
    assert len(statements) == 2


def test_location_paging_decrypts_only_the_requested_database_page(monkeypatch):
    """Unfiltered location pages decrypt one projected row rather than the whole table."""
    for number, name in enumerate(("Zulu ground", "Alpha ground", "Mike ground"), start=1):
        destination = create_destination(
            name=name,
            address=f"{number} Example Road",
            town_city="Exampleton",
            postcode=f"AB{number} 2CD",
            notes="Paging regression fixture",
        )
        save_destination_coordinates(destination["id"], 50 + number, -1 - number)
    from club_gift_aid_manager.models import types as model_types

    decryptions = 0
    original_decrypt = model_types.decrypt_value

    def counting_decrypt(value):
        """Count decrypted fields while retaining production behavior."""
        nonlocal decryptions
        if value is not None:
            decryptions += 1
        return original_decrypt(value)

    monkeypatch.setattr(model_types, "decrypt_value", counting_decrypt)
    statements = []
    engine = database.get_engine()

    def count_statement(*args):
        """Capture SQL emitted by the bounded location query."""
        statements.append(args[2])

    event.listen(engine, "before_cursor_execute", count_statement)
    try:
        destinations, total = list_destinations(limit=1, offset=1, with_total=True)
    finally:
        event.remove(engine, "before_cursor_execute", count_statement)

    assert total == 3
    assert [destination["name"] for destination in destinations] == ["Alpha ground"]
    assert set(destinations[0]) == {
        "id",
        "name",
        "address",
        "town_city",
        "postcode",
        "notes",
        "active",
        "home_ground",
    }
    assert decryptions == 5
    assert len(statements) == 2


def test_unpaginated_account_and_location_reads_skip_unused_counts():
    """Callers that do not request totals issue only their projected data query."""
    create_user(
        username="Count-free account",
        password="Browser-Page-Test3!",
        role="treasurer",
    )
    create_destination(name="Count-free location")
    statements = []
    engine = database.get_engine()

    def count_statement(*args):
        """Capture SQL to enforce the endpoint's bounded query budget."""
        statements.append(args[2])

    event.listen(engine, "before_cursor_execute", count_statement)
    try:
        assert len(list_users()) == 1
        assert len(list_destinations()) == 1
    finally:
        event.remove(engine, "before_cursor_execute", count_statement)

    assert len(statements) == 2
    assert all("COUNT(" not in statement.upper() for statement in statements)


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
