"""Trip entry, mileage claims, CSV imports, and ownership behavior.

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

def test_reimbursement_calculation_api_uses_trip_date_rate_schedule():
    """The amount API reports rates for the date the journey took place."""
    admin = signed_in_client("admin")
    create_rate_schedule(
        effective_from="2026-04-01",
        car_first_10000_pence=60,
        car_over_10000_pence=30,
        motorcycle_pence=35,
        bicycle_pence=25,
        passenger_pence=10,
    )

    before_change = admin.get(
        "/api/calculate?trip_date=2026-03-31&vehicle_type=car&miles_one_way=10&return_trip=false"
    ).get_json()
    on_change = admin.get(
        "/api/calculate?trip_date=2026-04-01&vehicle_type=car&miles_one_way=10&return_trip=false"
    ).get_json()
    motorcycle = admin.get(
        "/api/calculate?trip_date=2026-04-01&vehicle_type=motorcycle&miles_one_way=10&return_trip=false"
    ).get_json()
    bicycle_with_passenger = admin.get(
        "/api/calculate?trip_date=2026-04-01&vehicle_type=bicycle&miles_one_way=10&return_trip=false&passengers=1"
    ).get_json()

    assert before_change["rate_effective_from"] == "0001-01-01"
    assert before_change["total_amount_pence"] == 450
    assert on_change["rate_effective_from"] == "2026-04-01"
    assert on_change["total_amount_pence"] == 600
    assert motorcycle["total_amount_pence"] == 350
    assert bicycle_with_passenger["total_amount_pence"] == 350



def test_trip_import_rejects_excessive_row_count_before_route_work():
    """Oversized trip batches are rejected before external route calculations."""
    person = create_person(name="Bounded import person")
    origin = create_destination(name="Bounded origin")
    destination = create_destination(name="Bounded destination")
    header = "person_id,origin_id,destination_id,trip_date,vehicle_type,declaration_accepted\n"
    row = f"{person['id']},{origin['id']},{destination['id']},2026-11-01,car,yes\n"

    response = signed_in_client().post(
        "/trips/import",
        data={"file": (BytesIO((header + row * 1001).encode("utf-8")), "trips.csv")},
        headers={"X-CSRF-Token": "test-csrf-token"},
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert "more than 1000 data rows" in response.get_json()["error"]
    assert db_module.list_trips() == []



def test_volunteer_is_limited_to_own_trips_and_cannot_read_finances():
    """Volunteers can see only their own profile and trips, never finance records."""
    person = create_person(name="Volunteer", role="volunteer")
    other_person = create_person(name="Other volunteer", role="volunteer")
    own_trip = create_trip(
        person_id=person["id"],
        vehicle_type="car",
        trip_date=date(2026, 4, 10),
        miles_one_way=10,
        return_trip=False,
        declaration_text="Required declaration.",
    )
    other_trip = create_trip(
        person_id=other_person["id"],
        vehicle_type="car",
        trip_date=date(2026, 4, 11),
        miles_one_way=10,
        return_trip=False,
        declaration_text="Required declaration.",
    )
    destination = create_destination(name="Volunteer destination")
    client = signed_in_client("volunteer", person["id"])
    with client:
        visible_people = client.get("/people").get_json()["items"]
        assert [entry["id"] for entry in visible_people] == [person["id"]]
        assert client.get(f"/people/{other_person['id']}").status_code == 404
        trip_list = client.get("/trips").get_json()["items"]
        assert [trip["id"] for trip in trip_list] == [own_trip["id"]]
        assert client.get(f"/trips/{other_trip['id']}").status_code == 404
        assert client.get("/donations").status_code == 403
        assert client.get("/reports/summary").status_code == 403
        assert client.get("/reports/payment-reconciliation").status_code == 403
        create_response = client.post(
            "/trips",
            json={
                "person_id": other_person["id"],
                "origin_id": destination["id"],
                "destination_id": destination["id"],
                "trip_date": "2026-04-12",
                "vehicle_type": "car",
                "miles_one_way": 8,
                "declaration_accepted": True,
            },
            headers={"X-CSRF-Token": "test-csrf-token"},
        )
        assert create_response.status_code == 201
        assert create_response.get_json()["person_id"] == person["id"]



def test_treasurer_trip_creation_is_limited_to_linked_person():
    """A non-admin treasurer cannot assign a trip to another person's record."""
    own_person = create_person(name="Treasurer")
    other_person = create_person(name="Other person")
    destination = create_destination(name="Treasurer destination")
    client = signed_in_client("treasurer", own_person["id"])
    response = client.post(
        "/trips",
        json={
            "person_id": other_person["id"],
            "origin_id": destination["id"],
            "destination_id": destination["id"],
            "trip_date": "2026-04-12",
            "vehicle_type": "car",
            "miles_one_way": 8,
            "declaration_accepted": True,
        },
        headers={"X-CSRF-Token": "test-csrf-token"},
    )

    assert response.status_code == 201
    assert response.get_json()["person_id"] == own_person["id"]



def test_trip_csv_admin_can_import_for_multiple_people(monkeypatch):
    """Admins may assign imported trips using person names or numeric IDs."""
    admin = signed_in_client()
    person = create_person(name="CSV Claimant")
    destination = create_destination(name="CSV Ground")
    monkeypatch.setattr(
        app_module,
        "calculate_route_distance",
        lambda origin_id, destination_id: {"distance_miles": 18.4},
    )
    csv_data = (
        "person_name,origin_id,destination_id,trip_date,vehicle_type,miles_one_way,declaration_accepted\n"
        f"CSV Claimant,{destination['id']},{destination['id']},2026-05-01,car,12.5,yes\n"
    )
    response = admin.post(
        "/trips/import",
        data={"file": (BytesIO(csv_data.encode()), "trips.csv")},
        headers={"X-CSRF-Token": "test-csrf-token"},
    )

    assert response.status_code == 201
    assert response.get_json()["imported"] == 1
    assert response.get_json()["items"][0]["person_id"] == person["id"]
    assert response.get_json()["items"][0]["miles_one_way"] == 18.4



def test_trip_csv_non_admin_imports_only_for_linked_person(monkeypatch):
    """A non-admin's CSV is assigned to their linked person without a CSV person column."""
    own_person = create_person(name="Uploader")
    other_person = create_person(name="Not the uploader")
    destination = create_destination(name="Personal CSV Ground")
    monkeypatch.setattr(
        app_module,
        "calculate_route_distance",
        lambda origin_id, destination_id: {"distance_miles": 3.7},
    )
    client = signed_in_client("treasurer", own_person["id"])
    csv_data = (
        "origin_id,destination_id,trip_date,vehicle_type,miles_one_way,declaration_accepted\n"
        f"{destination['id']},{destination['id']},2026-05-02,bicycle,3,yes\n"
    )
    response = client.post(
        "/trips/import",
        data={"file": (BytesIO(csv_data.encode()), "trips.csv")},
        headers={"X-CSRF-Token": "test-csrf-token"},
    )
    assert response.status_code == 201
    assert response.get_json()["items"][0]["person_id"] == own_person["id"]
    assert response.get_json()["items"][0]["person_id"] != other_person["id"]
    assert response.get_json()["items"][0]["miles_one_way"] == 3.7

    forbidden = client.post(
        "/trips/import",
        data={
            "file": (
                BytesIO(
                    (
                        "person_id,origin_id,destination_id,trip_date,vehicle_type,miles_one_way,declaration_accepted\n"
                        f"{other_person['id']},{destination['id']},{destination['id']},2026-05-03,car,4,yes\n"
                    ).encode()
                ),
                "trips.csv",
            )
        },
        headers={"X-CSRF-Token": "test-csrf-token"},
    )
    assert forbidden.status_code == 403



def test_trip_csv_validation_is_atomic(monkeypatch):
    """A missing mandatory field in a later row prevents all trip inserts."""
    admin = signed_in_client()
    person = create_person(name="Atomic CSV Claimant")
    destination = create_destination(name="Atomic CSV Ground")
    monkeypatch.setattr(
        app_module,
        "calculate_route_distance",
        lambda origin_id, destination_id: {"distance_miles": 5.0},
    )
    csv_data = (
        "person_id,origin_id,destination_id,trip_date,vehicle_type,miles_one_way,declaration_accepted\n"
        f"{person['id']},{destination['id']},{destination['id']},2026-05-04,car,5,yes\n"
        f"{person['id']},{destination['id']},{destination['id']},,car,7,yes\n"
    )

    response = admin.post(
        "/trips/import",
        data={"file": (BytesIO(csv_data.encode()), "trips.csv")},
        headers={"X-CSRF-Token": "test-csrf-token"},
    )

    assert response.status_code == 400
    assert response.get_json()["errors"] == [
        "Row 3: is missing required field(s): trip_date."
    ]
    assert db_module.list_trips() == []



@pytest.mark.parametrize(
    ("csv_data", "expected_error"),
    [
        ("", "required trip columns"),
        (
            "person_id,person_id,origin_id,destination_id,trip_date,vehicle_type,declaration_accepted\n",
            "duplicate column",
        ),
        (
            "person_id,origin_id,destination_id,trip_date,vehicle_type,declaration_accepted,extra\n",
            "Unknown CSV column",
        ),
        ("person_id,origin_id\n", "Required columns are"),
        (
            "origin_id,destination_id,trip_date,vehicle_type,declaration_accepted\n",
            "must include a person_id or person_name",
        ),
        (
            "person_id,person_name,origin_id,destination_id,trip_date,vehicle_type,declaration_accepted\n",
            "either person_id or person_name",
        ),
    ],
)
def test_trip_csv_rejects_invalid_headers(csv_data, expected_error):
    """Malformed or unauthorized CSV schemas are rejected before import."""
    client = signed_in_client()
    response = client.post(
        "/trips/import",
        data={"file": (BytesIO(csv_data.encode()), "trips.csv")},
        headers={"X-CSRF-Token": "test-csrf-token"},
    )
    assert response.status_code == 400
    assert expected_error.lower() in response.get_json()["error"].lower()
    assert db_module.list_trips() == []



@pytest.mark.parametrize(
    ("row", "expected_error"),
    [
        ("bad,1,1,2026-05-01,car,2,yes", "valid person_id"),
        ("999,1,1,2026-05-01,car,0,yes,false", "does not exist"),
        ("1,1,1,not-a-date,car,0,yes,false", "invalid person, date"),
        ("1,not-an-id,1,2026-05-01,car,0,yes,false", "invalid person, date"),
        ("1,1,1,2026-05-01,plane,0,yes,false", "invalid vehicle_type"),
        ("1,1,1,2026-05-01,car,-2,yes,false", "passenger count"),
        ("1,1,1,2026-05-01,car,0,yes,nope", "invalid return_trip"),
    ],
)
def test_trip_csv_rejects_invalid_rows(row, expected_error):
    """Invalid assignments and trip values do not create partial records."""
    person = create_person(name="CSV validation claimant")
    destination = create_destination(name="CSV validation ground")
    client = signed_in_client()
    csv_data = (
        "person_id,origin_id,destination_id,trip_date,vehicle_type,passengers,declaration_accepted,return_trip\n"
        f"{row}\n"
    )
    response = client.post(
        "/trips/import",
        data={"file": (BytesIO(csv_data.encode()), "trips.csv")},
        headers={"X-CSRF-Token": "test-csrf-token"},
    )
    assert response.status_code == 400
    errors = " ".join(response.get_json()["errors"]).lower()
    assert expected_error.lower() in errors
    assert db_module.list_trips() == []
    assert person["id"] > 0 and destination["id"] > 0



def test_trip_csv_returns_all_row_and_route_errors_without_partial_import(monkeypatch):
    """Every rejected row is listed, including routes that cannot be calculated."""
    admin = signed_in_client()
    person = create_person(name="Trip CSV errors")
    origin = create_destination(name="CSV origin")
    destination_one = create_destination(name="CSV destination one")
    destination_two = create_destination(name="CSV destination two")

    def fake_route(origin_id, destination_id):
        if destination_id == destination_two["id"]:
            raise RouteLookupError("No driving route was found between these locations.")
        return {"distance_miles": 11.2}

    monkeypatch.setattr(app_module, "calculate_route_distance", fake_route)
    csv_data = (
        "person_id,origin_id,destination_id,trip_date,vehicle_type,declaration_accepted\n"
        f"{person['id']},{origin['id']},{destination_one['id']},not-a-date,car,yes\n"
        f"{person['id']},{origin['id']},{destination_one['id']},2026-05-01,plane,yes\n"
        f"{person['id']},{origin['id']},{destination_two['id']},2026-05-02,car,yes\n"
    )

    response = admin.post(
        "/trips/import",
        data={"file": (BytesIO(csv_data.encode()), "trips.csv")},
        headers={"X-CSRF-Token": "test-csrf-token"},
    )

    assert response.status_code == 422
    assert response.get_json()["errors"] == [
        "Row 2: has an invalid person, date, location ID, or passenger count.",
        "Row 3: has an invalid vehicle_type; use car, motorcycle, or bicycle.",
        "Row 4: road distance could not be calculated: No driving route was found between these locations.",
    ]
    assert db_module.list_trips() == []



def test_payment_can_link_multiple_full_trips_once():
    """One payment may cover many full trips, but no trip can be paid twice."""
    person = create_person(name="Trip payment volunteer", role="volunteer")
    trips = [
        create_trip(
            person_id=person["id"],
            vehicle_type="car",
            trip_date=date(2026, 4, day),
            miles_one_way=10,
            return_trip=True,
            declaration_text="I declare this trip was for club business.",
        )
        for day in (10, 11)
    ]

    with pytest.raises(ValueError, match="must equal the total reimbursement"):
        create_payment(
            person_id=person["id"],
            amount_pence=1801,
            trip_ids=[trip["id"] for trip in trips],
        )

    payment = create_payment(
        person_id=person["id"],
        amount_pence=1800,
        trip_ids=[trip["id"] for trip in trips],
    )

    assert payment["trip_ids"] == [trip["id"] for trip in trips]
    listed_payment = list_payments(person["id"])[0]
    assert [trip["id"] for trip in listed_payment["linked_trips"]] == payment["trip_ids"]
    assert all(trip["payment_id"] == payment["id"] for trip in db_module.list_trips(person["id"]))
    with pytest.raises(ValueError, match="already been linked"):
        create_payment(
            person_id=person["id"],
            amount_pence=4500,
            trip_ids=[trips[0]["id"]],
        )



def test_payment_api_links_selected_trips_to_the_payment():
    """The HTTP payment endpoint persists links and rejects a duplicate payment."""
    person = create_person(name="Payment endpoint volunteer", role="volunteer")
    trip = create_trip(
        person_id=person["id"],
        vehicle_type="car",
        trip_date=date(2026, 4, 12),
        miles_one_way=12,
        return_trip=True,
        declaration_text="I declare this trip was for club business.",
    )
    client = signed_in_client()
    headers = {"X-CSRF-Token": "test-csrf-token"}
    response = client.post(
        "/payments",
        json={
            "person_id": person["id"],
            "amount_pence": 1080,
            "trip_ids": [trip["id"]],
        },
        headers=headers,
    )
    assert response.status_code == 201
    payment = response.get_json()
    assert payment["trip_ids"] == [trip["id"]]
    listed_payment = client.get("/payments").get_json()["items"][0]
    assert listed_payment["linked_trips"][0]["id"] == trip["id"]
    listed_trip = next(
        item for item in client.get("/trips").get_json()["items"]
        if item["id"] == trip["id"]
    )
    assert listed_trip["payment_id"] == payment["id"]
    duplicate = client.post(
        "/payments",
        json={
            "person_id": person["id"],
            "amount_pence": 1080,
            "trip_ids": [trip["id"]],
        },
        headers=headers,
    )
    assert duplicate.status_code == 400



def test_rate_schedules_are_complete_and_selected_by_trip_occurrence_date():
    """A later-entered trip still uses the schedule effective on its trip date."""
    create_rate_schedule(
        effective_from="2026-04-01",
        car_first_10000_pence=60,
        car_over_10000_pence=30,
        motorcycle_pence=35,
        bicycle_pence=25,
        passenger_pence=10,
    )

    before_change = get_rate_schedule_for_date("2026-03-31")
    on_change = get_rate_schedule_for_date("2026-04-01")
    schedules = list_rate_schedules()

    assert before_change["effective_from"] == "0001-01-01"
    assert on_change["effective_from"] == "2026-04-01"
    assert schedules[0]["effective_to"] == "2026-03-31"
    assert schedules[1]["effective_to"] is None

    person = create_person(name="Rate history volunteer")
    old_trip = create_trip(
        person_id=person["id"],
        vehicle_type="car",
        trip_date=date(2026, 3, 31),
        miles_one_way=10,
        return_trip=False,
        declaration_text="I accept the mileage declaration.",
    )
    new_trip = create_trip(
        person_id=person["id"],
        vehicle_type="car",
        trip_date=date(2026, 4, 1),
        miles_one_way=10,
        return_trip=False,
        declaration_text="I accept the mileage declaration.",
    )

    old_payment = create_payment(
        person_id=person["id"],
        amount_pence=450,
        trip_ids=[old_trip["id"]],
    )
    new_payment = create_payment(
        person_id=person["id"],
        amount_pence=600,
        trip_ids=[new_trip["id"]],
    )

    assert old_payment["amount_pence"] == 450
    assert new_payment["amount_pence"] == 600



def test_volunteer_dashboard_overview_is_limited_to_own_trip_information():
    """Volunteer dashboard data never includes club-wide financial or admin details."""
    own_person = create_person(name="Own dashboard person")
    other_person = create_person(name="Other dashboard person")
    create_trip(
        person_id=own_person["id"],
        vehicle_type="car",
        trip_date=date(2026, 10, 1),
        miles_one_way=10,
        return_trip=False,
        declaration_text="I accept the mileage declaration.",
    )
    create_trip(
        person_id=other_person["id"],
        vehicle_type="car",
        trip_date=date(2026, 10, 2),
        miles_one_way=20,
        return_trip=False,
        declaration_text="I accept the mileage declaration.",
    )
    for day in (3, 4, 5):
        create_trip(
            person_id=own_person["id"],
            vehicle_type="car",
            trip_date=date(2026, 10, day),
            miles_one_way=10,
            return_trip=False,
            declaration_text="I accept the mileage declaration.",
        )

    response = signed_in_client("volunteer", person_id=own_person["id"]).get("/dashboard/overview")

    assert response.status_code == 200
    data = response.get_json()
    assert data["trip_count"] == 4
    assert [trip["trip_date"] for trip in data["recent_trips"]] == [
        "2026-10-05",
        "2026-10-04",
        "2026-10-03",
    ]
    assert "trips_without_payment" not in data
    assert "recent_activity" not in data



def test_calculate_trip_amounts_for_car():
    """Mileage and passenger components combine into the expected pence total."""
    result = calculate_trip_amounts(
        vehicle_type="car",
        trip_date=date(2026, 4, 10),
        miles_one_way=20,
        return_trip=True,
        passengers=2,
    )

    assert result["tax_year"] == "2026/27"
    assert result["total_miles"] == 40.0
    assert result["passenger_amount_pence"] == 400
    assert result["mileage_amount_pence"] == 1800
    assert result["total_amount_pence"] == 2200

