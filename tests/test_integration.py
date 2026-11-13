"""End-to-end workflows spanning several application domains.

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

def test_every_decorated_route_rejects_every_disallowed_role():
    """Exercise the complete negative role/route matrix from decorator metadata."""
    person = create_person(name="Authorization matrix volunteer")
    clients = {
        "admin": signed_in_client("admin"),
        "treasurer": signed_in_client("treasurer"),
        "volunteer": signed_in_client("volunteer", person["id"]),
    }
    adapter = app.url_map.bind("localhost")
    checked = []

    for rule in app.url_map.iter_rules():
        view = app.view_functions[rule.endpoint]
        allowed_roles = getattr(view, "required_roles", None)
        if allowed_roles is None:
            continue
        values = {argument: 1 for argument in rule.arguments}
        path = adapter.build(rule.endpoint, values)
        methods = sorted(rule.methods - {"HEAD", "OPTIONS"})
        for role, client in clients.items():
            if role in allowed_roles:
                continue
            for method in methods:
                response = client.open(
                    path,
                    method=method,
                    headers={"X-CSRF-Token": "test-csrf-token"},
                )
                assert response.status_code == 403, (
                    f"{role} unexpectedly reached {method} {path} "
                    f"({rule.endpoint})"
                )
                assert response.get_json() == {
                    "error": "You do not have permission to perform this action."
                }
                checked.append((rule.endpoint, method, role))

    assert checked, "The authorization matrix did not discover protected routes."



def test_crud_person_trip_and_payment_flow():
    """The persistence layer stores a person's trip, donation, and payment."""
    person = create_person(name="Sam Parent", email="sam@example.com", role="parent")
    trip = create_trip(
        person_id=person["id"],
        vehicle_type="car",
        trip_date=date(2026, 4, 10),
        miles_one_way=20,
        return_trip=True,
        passengers=1,
        declaration_accepted=True,
        declaration_text="I declare that I have taken other parents' children on this journey.",
    )
    assert trip["tax_year"] == "2026/27"
    donation = create_donation(person_id=person["id"], amount_pence=5000, gift_aid_eligible=True)
    payment = create_payment(person_id=person["id"], amount_pence=2200, method="bank transfer")
    assert donation["amount_pence"] == 5000
    assert payment["amount_pence"] == 2200



def test_flask_routes_support_full_crud():
    """Representative create routes and dashboard reporting work through Flask."""
    client = signed_in_client()
    csrf = {"X-CSRF-Token": "test-csrf-token"}
    destination = create_destination(name="Northside RFC")
    with client:
        person_response = client.post("/people", json={"name": "Alice Coach", "role": "coach"}, headers=csrf)
        assert person_response.status_code == 201
        person = person_response.get_json()

        trip_response = client.post(
            "/trips",
            json={
                "person_id": person["id"],
                "origin_id": destination["id"],
                "destination_id": destination["id"],
                "trip_date": "2026-04-10",
                "vehicle_type": "car",
                "miles_one_way": 20,
                "return_trip": True,
                "passengers": 1,
                "declaration_accepted": True,
            },
            headers=csrf,
        )
        assert trip_response.status_code == 201
        assert trip_response.get_json()["destination_id"] == destination["id"]
        audit_events = client.get("/admin/audit").get_json()["items"]
        created_records = {
            (event["entity_type"], event["entity_id"])
            for event in audit_events
            if event["action"] == "created"
        }
        assert ("person", str(person["id"])) in created_records
        assert ("trip", str(trip_response.get_json()["id"])) in created_records

        report_response = client.get("/reports/summary")
        assert report_response.status_code == 200
        summary = report_response.get_json()
        assert summary["people"] >= 1
        assert summary["trips"] >= 1

