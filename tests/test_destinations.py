"""Destination masterfile, geocoding, routing, and CSV import behavior.

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

def test_admin_can_import_locations_from_csv():
    """CSV import accepts supported fields and records the selected home ground."""
    client = signed_in_client()
    response = client.post(
        "/destinations/import",
        data={
            "file": (BytesIO(
                b"name,address,town_city,postcode,notes,home_ground\n"
                b"Home Club,Main Road,Exampletown,AB1 2CD,Club ground,yes\n"
                b"Opponent RFC,High Street,Otherplace,XY1 3ZZ,Away ground,no\n"
            ), "locations.csv"),
        },
        headers={"X-CSRF-Token": "test-csrf-token"},
    )

    assert response.status_code == 201
    assert response.get_json()["imported"] == 2
    locations = {location["name"]: location for location in list_destinations()}
    assert locations["Home Club"]["address"] == "Main Road"
    assert locations["Home Club"]["home_ground"] == 1
    assert locations["Opponent RFC"]["home_ground"] == 0



def test_destination_import_rejects_excessive_row_count():
    """Compact CSV files cannot monopolise a worker with unbounded row counts."""
    csv_data = "name\n" + "".join(f"Location {number}\n" for number in range(1001))

    response = signed_in_client().post(
        "/destinations/import",
        data={"file": (BytesIO(csv_data.encode("utf-8")), "locations.csv")},
        headers={"X-CSRF-Token": "test-csrf-token"},
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert "more than 1000 data rows" in response.get_json()["error"]
    assert list_destinations() == []



@pytest.mark.parametrize(
    ("csv_data", "expected_error"),
    [
        ("", "mandatory"),
        ("name,name\nGround,Other\n", "duplicate column"),
        ("name,unexpected\nGround,value\n", "Unknown CSV column"),
        ("address\nMain Road\n", "mandatory"),
        ("name\n", "does not contain any locations"),
        ("name,home_ground\nGround,maybe\n", "invalid home_ground"),
        ("name,notes\nGround,one,two\n", "more values"),
        ("name\n\"Unclosed\n", "unexpected end"),
    ],
)
def test_location_csv_rejects_invalid_files(csv_data, expected_error):
    """Location uploads reject malformed schemas and rows without saving records."""
    client = signed_in_client()
    response = client.post(
        "/destinations/import",
        data={"file": (BytesIO(csv_data.encode()), "locations.csv")},
        headers={"X-CSRF-Token": "test-csrf-token"},
    )
    assert response.status_code == 400
    assert expected_error.lower() in response.get_json()["error"].lower()
    assert list_destinations() == []



def test_location_csv_rejects_multiple_home_grounds():
    """A location upload cannot assign the club home-ground flag more than once."""
    client = signed_in_client()
    response = client.post(
        "/destinations/import",
        data={
            "file": (
                BytesIO(b"name,home_ground\nHome One,yes\nHome Two,true\n"),
                "locations.csv",
            )
        },
        headers={"X-CSRF-Token": "test-csrf-token"},
    )
    assert response.status_code == 400
    assert "only one row" in response.get_json()["error"].lower()
    assert list_destinations() == []



def test_destinations_masterfile_and_trip_selection_roles():
    """Home-ground rules, trip location validation, and role-based choices hold."""
    admin = signed_in_client()
    csrf = {"X-CSRF-Token": "test-csrf-token"}
    with admin:
        created = admin.post(
            "/destinations",
            json={
                "name": "West Borough RFC",
                "town_city": "West Borough",
                "postcode": "AB1 2CD",
                "home_ground": True,
            },
            headers=csrf,
        )
        assert created.status_code == 201
        destination = created.get_json()
        assert destination["active"] == 1
        assert destination["home_ground"] == 1

        person = create_person(name="Claimant")
        trip = admin.post(
            "/trips",
            json={
                "person_id": person["id"],
                "origin_id": destination["id"],
                "destination_id": destination["id"],
                "trip_date": "2026-05-01",
                "vehicle_type": "car",
                "miles_one_way": 15,
                "declaration_accepted": True,
            },
            headers=csrf,
        )
        assert trip.status_code == 201
        assert trip.get_json()["destination_id"] == destination["id"]
        assert trip.get_json()["origin_id"] == destination["id"]
        listed_trip = next(row for row in admin.get("/trips").get_json()["items"] if row["id"] == trip.get_json()["id"])
        assert listed_trip["destination_name"] == "West Borough RFC"
        assert listed_trip["origin_name"] == "West Borough RFC"

        rejected = admin.post(
            "/trips",
            json={
                "person_id": person["id"],
                "origin_id": destination["id"],
                "trip_date": "2026-05-02",
                "vehicle_type": "car",
                "miles_one_way": 15,
                "declaration_accepted": True,
            },
            headers=csrf,
        )
        assert rejected.status_code == 400

        missing_origin = admin.post(
            "/trips",
            json={
                "person_id": person["id"],
                "destination_id": destination["id"],
                "trip_date": "2026-05-03",
                "vehicle_type": "car",
                "miles_one_way": 15,
                "declaration_accepted": True,
            },
            headers=csrf,
        )
        assert missing_origin.status_code == 400

        alternate_home = admin.post(
            "/destinations",
            json={"name": "Club training ground"},
            headers=csrf,
        ).get_json()
        designated_home = admin.put(
            f"/destinations/{alternate_home['id']}",
            json={"home_ground": True},
            headers=csrf,
        )
        assert designated_home.get_json()["home_ground"] == 1
        destinations = admin.get("/destinations").get_json()["items"]
        assert sum(item["home_ground"] for item in destinations) == 1
        assert next(item for item in destinations if item["id"] == destination["id"])["home_ground"] == 0
        claim_page = admin.get("/claim")
        assert f'<option value="{alternate_home["id"]}" selected>' in claim_page.get_data(as_text=True)

        inactive = admin.put(
            f"/destinations/{alternate_home['id']}",
            json={"active": False},
            headers=csrf,
        )
        assert inactive.status_code == 200
        assert inactive.get_json()["home_ground"] == 0

        cannot_delete_in_use = admin.delete(
            f"/destinations/{destination['id']}",
            headers=csrf,
        )
        assert cannot_delete_in_use.status_code == 409



def test_route_distance_api_falls_back_to_postcode_when_venue_search_fails(monkeypatch):
    """When a venue query misses, the geocoder retries using the postcode alone."""
    origin = create_destination(
        name="old brods",
        address="Woodhead, Hipperholme",
        town_city="hipperhome",
        postcode="HX3 8JU",
    )
    destination = create_destination(
        name="Doncaster",
        address="Doncaster",
        town_city="Doncaster",
    )
    client = signed_in_client()
    request_urls = []
    responses = iter(
        [
            [],
            [{"lat": "53.7314700", "lon": "-1.8162200"}],
            [{"lat": "53.5227681", "lon": "-1.1335312"}],
            {"code": "Ok", "routes": [{"distance": 32186.88}]},
        ]
    )

    def fake_request_json(url):
        """Record the requested URL and return the next deterministic fixture."""
        request_urls.append(url)
        return next(responses)

    monkeypatch.setattr(routing_module, "_request_json", fake_request_json)
    monkeypatch.setattr(routing_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(routing_module, "_last_geocoder_request", 0.0)

    response = client.get(
        f"/api/route-distance?origin_id={origin['id']}&destination_id={destination['id']}"
    )

    assert response.status_code == 200
    assert response.get_json()["distance_miles"] == 20.0
    assert "old+brods" in request_urls[0]
    assert "HX3+8JU" in request_urls[1]
    assert db_module.get_destination(origin["id"])["latitude"] == 53.73147



def test_destination_management_page_and_access_are_role_gated():
    """Staff manage all locations while volunteers can select active ones only."""
    volunteer_person = create_person(name="Volunteer", role="volunteer")
    volunteer = signed_in_client("volunteer", volunteer_person["id"])
    destination = create_destination(name="Active opponent")
    inactive = create_destination(name="Inactive opponent")
    from club_gift_aid_manager.db import update_destination
    update_destination(inactive["id"], active=False)

    with volunteer:
        assert volunteer.get("/destinations/page").status_code == 403
        choices = volunteer.get("/destinations").get_json()["items"]
        assert [item["id"] for item in choices] == [destination["id"]]
        assert volunteer.post(
            "/destinations",
            json={"name": "Forbidden RFC"},
            headers={"X-CSRF-Token": "test-csrf-token"},
        ).status_code == 403



def test_location_csv_rejects_missing_mandatory_names_without_partial_import():
    """Every non-empty CSV row needs a name, and validation happens before insertion."""
    client = signed_in_client()
    response = client.post(
        "/destinations/import",
        data={
            "file": (BytesIO(b"name,address\nValid Ground,Main Road\n,Other Road\n"), "locations.csv"),
        },
        headers={"X-CSRF-Token": "test-csrf-token"},
    )

    assert response.status_code == 400
    assert "Row 3" in response.get_json()["error"]
    assert list_destinations() == []



def test_location_csv_duplicate_name_rolls_back_whole_import():
    """A name conflict does not leave earlier CSV rows saved."""
    create_destination(name="Already Exists")
    client = signed_in_client()
    response = client.post(
        "/destinations/import",
        data={
            "file": (BytesIO(b"name\nNew Ground\nAlready Exists\n"), "locations.csv"),
        },
        headers={"X-CSRF-Token": "test-csrf-token"},
    )

    assert response.status_code == 409
    assert "Already Exists" in response.get_json()["error"]
    assert {location["name"] for location in list_destinations()} == {"Already Exists"}



def test_location_csv_duplicate_names_skip_later_rows():
    """A repeated CSV name skips later rows and imports the first occurrence."""
    client = signed_in_client()
    response = client.post(
        "/destinations/import",
        data={
            "file": (BytesIO(b"name\nNew Ground\nnew ground\n"), "locations.csv"),
        },
        headers={"X-CSRF-Token": "test-csrf-token"},
    )

    assert response.status_code == 201
    assert response.get_json()["imported"] == 1
    assert response.get_json()["skipped_duplicate_rows"] == [3]
    assert [location["name"] for location in list_destinations()] == ["New Ground"]

