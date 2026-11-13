"""Regression tests for hostile and accidentally malformed form values.

Each case below reproduces an input accepted before the validation hardening.
The public API contract remains a 400 response containing ``{"error": ...}``.
"""

import pytest

from club_gift_aid_manager.db import create_destination, create_person
from tests.helpers import signed_in_client


CSRF = {"X-CSRF-Token": "test-csrf-token"}


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/people", {"name": "   "}),
        ("/people", {"name": "Email probe", "email": "not-an-email"}),
        ("/people", {"name": "Boolean probe", "active": "banana"}),
        ("/destinations", {"name": "Boolean destination", "home_ground": "banana"}),
    ],
)
def test_master_data_create_routes_reject_malformed_fields(path, payload):
    """Names, emails, and Boolean choices are validated before insertion."""
    response = signed_in_client("admin").post(path, json=payload, headers=CSRF)

    assert response.status_code == 400
    assert set(response.get_json()) == {"error"}


@pytest.mark.parametrize(
    ("path", "payload_factory"),
    [
        ("/declarations", lambda person: {"person_id": person["id"], "full_name": "   "}),
        ("/declarations", lambda person: {"person_id": person["id"], "full_name": "Date probe", "declaration_date": "31/02/2026"}),
        ("/donations", lambda person: {"person_id": person["id"], "amount_pence": 100, "donated_date": "yesterday"}),
        ("/donations", lambda person: {"person_id": person["id"], "amount_pence": 100, "gift_aid_eligible": "banana"}),
        ("/payments", lambda person: {"person_id": person["id"], "amount_pence": 100, "paid_date": "2026-99-99", "trip_ids": []}),
        ("/donations", lambda person: {"person_id": person["id"], "amount_pence": -1}),
        ("/payments", lambda person: {"person_id": person["id"], "amount_pence": -1, "trip_ids": []}),
    ],
)
def test_finance_create_routes_reject_malformed_fields(path, payload_factory):
    """Finance dates, amounts, names, and flags fail with the common format."""
    person = create_person(name="Validation person")
    response = signed_in_client("admin").post(
        path, json=payload_factory(person), headers=CSRF,
    )

    assert response.status_code == 400
    assert set(response.get_json()) == {"error"}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("return_trip", "banana"),
        ("passengers", -1),
        ("miles_one_way", "NaN"),
        ("miles_one_way", -0.1),
    ],
)
def test_trip_create_rejects_invalid_boolean_counts_and_mileage(field, value):
    """A trip cannot contain ambiguous flags or impossible measurements."""
    person = create_person(name="Trip validation person")
    origin = create_destination(name="Validation origin")
    destination = create_destination(name="Validation destination")
    payload = {
        "person_id": person["id"],
        "origin_id": origin["id"],
        "destination_id": destination["id"],
        "trip_date": "2026-11-01",
        "vehicle_type": "car",
        "miles_one_way": 1,
        "return_trip": False,
        "passengers": 0,
        "declaration_accepted": True,
    }
    payload[field] = value

    response = signed_in_client("admin").post("/trips", json=payload, headers=CSRF)

    assert response.status_code == 400
    assert set(response.get_json()) == {"error"}
