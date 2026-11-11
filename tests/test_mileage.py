"""Regression tests for business rules, Flask routes, permissions, and persistence."""

from datetime import date
from io import BytesIO
import re
from pathlib import Path

import pytest

import app as app_module
import club_gift_aid_manager.db as db_module
import club_gift_aid_manager.routing as routing_module
from app import app
from club_gift_aid_manager.config import get_declaration_text, update_declaration_text
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
    list_destinations,
    list_declarations,
    list_donations,
    list_payment_reconciliation,
    list_payments,
    save_destination_coordinates,
    update_donation,
)
from club_gift_aid_manager.services import calculate_tax_year, calculate_trip_amounts, validate_declaration


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    """Give each test a fresh temporary database so persisted state cannot leak."""
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.sqlite3")
    create_app_database()


def signed_in_client(role="admin", person_id=None):
    """Build a test client with a database user and known session CSRF token."""
    client = app.test_client()
    user = create_user(
        username=f"test-{role}-{person_id or 'user'}",
        password="a-secure-test-password",
        role=role,
        person_id=person_id,
    )
    with client.session_transaction() as browser_session:
        browser_session["user_id"] = user["id"]
        browser_session["_csrf_token"] = "test-csrf-token"
    return client


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


def test_tax_year_for_2026_04_06_is_2026_27():
    """The first day of a UK tax year belongs to the new year."""
    assert calculate_tax_year(date(2026, 4, 6)) == "2026/27"


def test_tax_year_for_2025_04_05_is_2024_25():
    """The day before 6 April remains in the previous tax year."""
    assert calculate_tax_year(date(2025, 4, 5)) == "2024/25"


def test_car_claim_requires_declaration():
    """A claim cannot be submitted without accepted declaration wording."""
    with pytest.raises(ValueError, match="Declaration must be accepted"):
        validate_declaration(False, "I declare that I have taken other parents' children on this journey.")


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


def test_backdated_rate_schedule_closes_periods_without_replacing_later_schedule():
    """Adding a schedule between existing dates recomputes neighboring intervals."""
    rates = {
        "car_first_10000_pence": 60,
        "car_over_10000_pence": 30,
        "motorcycle_pence": 35,
        "bicycle_pence": 25,
        "passenger_pence": 10,
    }
    create_rate_schedule(effective_from="2027-01-01", **rates)
    create_rate_schedule(effective_from="2025-01-01", **rates)

    schedules = list_rate_schedules()

    assert [schedule["effective_from"] for schedule in schedules] == [
        "0001-01-01",
        "2025-01-01",
        "2027-01-01",
    ]
    assert [schedule["effective_to"] for schedule in schedules] == [
        "2024-12-31",
        "2026-12-31",
        None,
    ]
    assert get_rate_schedule_for_date(date(2026, 12, 31))["effective_from"] == "2025-01-01"
    assert get_rate_schedule_for_date(date(2027, 1, 1))["effective_from"] == "2027-01-01"


@pytest.mark.parametrize("effective_from", ["not-a-date", "2026-02-30", "0001-01-01"])
def test_rate_schedule_rejects_malformed_or_reserved_effective_dates(effective_from):
    """Malformed dates and the built-in baseline date cannot be overwritten."""
    with pytest.raises(ValueError, match="Effective date|original baseline"):
        create_rate_schedule(
            effective_from=effective_from,
            car_first_10000_pence=45,
            car_over_10000_pence=25,
            motorcycle_pence=24,
            bicycle_pence=20,
            passenger_pence=5,
        )


@pytest.mark.parametrize("invalid_rate", [-1, True, 1.5, 2**63])
def test_rate_schedule_rejects_negative_non_integer_and_unstorable_rates(invalid_rate):
    """Rate storage accepts only non-negative whole pence within SQLite integer range."""
    rates = {
        "car_first_10000_pence": 45,
        "car_over_10000_pence": 25,
        "motorcycle_pence": 24,
        "bicycle_pence": 20,
        "passenger_pence": 5,
    }
    rates["passenger_pence"] = invalid_rate

    with pytest.raises(ValueError):
        create_rate_schedule(effective_from="2027-01-01", **rates)


def test_admin_can_manage_rate_schedules_and_other_roles_cannot():
    """Only admins can view or add rates, and additions are audited."""
    admin = signed_in_client("admin")
    treasurer = signed_in_client("treasurer")
    volunteer_person = create_person(name="Rate schedule volunteer")
    volunteer = signed_in_client("volunteer", person_id=volunteer_person["id"])
    csrf = {"X-CSRF-Token": "test-csrf-token"}
    schedule_data = {
        "effective_from": "2027-01-01",
        "car_first_10000_pence": 50,
        "car_over_10000_pence": 25,
        "motorcycle_pence": 24,
        "bicycle_pence": 20,
        "passenger_pence": 5,
    }

    assert admin.get("/mileage-rates/page").status_code == 200
    assert treasurer.get("/mileage-rates/page").status_code == 403
    assert volunteer.get("/mileage-rates/page").status_code == 403
    assert treasurer.get("/admin/mileage-rates").status_code == 403
    assert volunteer.get("/admin/mileage-rates").status_code == 403

    created = admin.post("/admin/mileage-rates", json=schedule_data, headers=csrf)
    assert created.status_code == 201
    assert created.get_json()["car_first_10000_pence"] == 50
    duplicate = admin.post("/admin/mileage-rates", json=schedule_data, headers=csrf)
    assert duplicate.status_code == 409
    invalid = admin.post(
        "/admin/mileage-rates",
        json={**schedule_data, "bicycle_pence": -1},
        headers=csrf,
    )
    assert invalid.status_code == 400
    missing = admin.post(
        "/admin/mileage-rates",
        json={"effective_from": "2028-01-01"},
        headers=csrf,
    )
    assert missing.status_code == 400
    fractional = admin.post(
        "/admin/mileage-rates",
        json={**schedule_data, "car_first_10000_pence": 50.5},
        headers=csrf,
    )
    assert fractional.status_code == 400
    assert admin.get("/admin/mileage-rates").get_json()["items"][-1]["effective_from"] == "2027-01-01"

    audited = [
        event for event in admin.get("/admin/audit").get_json()["items"]
        if event["entity_type"] == "mileage_rate_schedule"
    ]
    assert len(audited) == 1
    assert audited[0]["action"] == "created"


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


def test_declaration_snapshot_is_not_changed_by_later_config_updates():
    """Changing current declaration text does not mutate an earlier snapshot."""
    original = get_declaration_text()
    snapshot = original
    update_declaration_text("Updated declaration wording")
    assert snapshot == original
    assert get_declaration_text() == "Updated declaration wording"


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


def test_donation_can_be_linked_to_reimbursement_without_netting_amounts():
    """Linking a donation supports reconciliation but does not net either amount."""
    person = create_person(name="Parent Volunteer", role="parent")
    payment = create_payment(
        person_id=person["id"],
        amount_pence=4550,
        paid_date="2026-10-10",
        method="bank transfer",
        reference="MILEAGE-104",
    )
    second_payment = create_payment(
        person_id=person["id"],
        amount_pence=2200,
        paid_date="2026-10-14",
        method="cash",
    )
    donation = create_donation(
        person_id=person["id"],
        amount_pence=2000,
        donated_date="2026-10-12",
        method="bank transfer",
        reference="DONATION-55",
        linked_payment_ids=[payment["id"], second_payment["id"]],
    )

    recorded_donation = list_donations(person["id"])[0]
    recorded_payments = {row["id"]: row for row in list_payments(person["id"])}
    assert recorded_donation["amount_pence"] == 2000
    assert recorded_donation["linked_payment_id"] == payment["id"]
    assert recorded_donation["linked_payment_ids"] == [payment["id"], second_payment["id"]]
    for related_payment in (payment, second_payment):
        assert recorded_payments[related_payment["id"]]["donated_back_pence"] == 2000
        assert recorded_payments[related_payment["id"]]["linked_donation_count"] == 1
    assert recorded_payments[payment["id"]]["amount_pence"] == 4550


def test_donation_cannot_link_to_another_persons_reimbursement():
    """A donation cannot be linked to a different person's payment."""
    donor = create_person(name="Donor", role="parent")
    other_parent = create_person(name="Another parent", role="parent")
    payment = create_payment(person_id=other_parent["id"], amount_pence=3000)
    with pytest.raises(ValueError, match="same person"):
        create_donation(
            person_id=donor["id"],
            amount_pence=1000,
            linked_payment_id=payment["id"],
        )


def test_donation_links_are_validated_and_replaceable_as_a_set():
    """Multiple payment links stay same-person, and edits replace the complete set."""
    donor = create_person(name="Multi-link donor")
    other_person = create_person(name="Other multi-link donor")
    payment_one = create_payment(person_id=donor["id"], amount_pence=1200)
    payment_two = create_payment(person_id=donor["id"], amount_pence=1400)
    foreign_payment = create_payment(person_id=other_person["id"], amount_pence=1600)
    donation = create_donation(
        person_id=donor["id"],
        amount_pence=500,
        linked_payment_ids=[payment_one["id"], payment_two["id"]],
    )

    with pytest.raises(ValueError, match="same person"):
        db_module.update_donation(
            donation["id"],
            linked_payment_ids=[payment_one["id"], foreign_payment["id"]],
        )

    updated = db_module.update_donation(
        donation["id"],
        linked_payment_ids=[payment_two["id"]],
    )
    assert updated["linked_payment_ids"] == [payment_two["id"]]
    cleared = db_module.update_donation(donation["id"], linked_payment_ids=[])
    assert cleared["linked_payment_ids"] == []


def test_donation_api_accepts_multiple_payment_links():
    """The donations API creates and updates multiple same-person references."""
    person = create_person(name="API multi-link donor")
    payment_one = create_payment(person_id=person["id"], amount_pence=1000)
    payment_two = create_payment(person_id=person["id"], amount_pence=2000)
    client = signed_in_client()
    csrf = {"X-CSRF-Token": "test-csrf-token"}
    created = client.post(
        "/donations",
        json={
            "person_id": person["id"],
            "amount_pence": 750,
            "linked_payment_ids": [payment_one["id"], payment_two["id"]],
        },
        headers=csrf,
    )
    assert created.status_code == 201
    donation_id = created.get_json()["id"]
    assert created.get_json()["linked_payment_ids"] == [payment_one["id"], payment_two["id"]]

    updated = client.put(
        f"/donations/{donation_id}",
        json={"linked_payment_ids": [payment_two["id"]]},
        headers=csrf,
    )
    assert updated.status_code == 200
    assert updated.get_json()["linked_payment_ids"] == [payment_two["id"]]
    assert 'multiple size="4"' in client.get("/donations/page").get_data(as_text=True)


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


def test_payment_reconciliation_summarizes_linked_donations_by_person():
    """Reconciliation counts payments and only donations linked to each payment."""
    volunteer = create_person(name="Volunteer One", role="volunteer")
    other_volunteer = create_person(name="Volunteer Two", role="volunteer")
    payment_one = create_payment(person_id=volunteer["id"], amount_pence=4550)
    payment_two = create_payment(person_id=volunteer["id"], amount_pence=2500)
    create_payment(person_id=other_volunteer["id"], amount_pence=1200)
    create_donation(
        person_id=volunteer["id"],
        amount_pence=1000,
        linked_payment_id=payment_one["id"],
    )
    create_donation(
        person_id=volunteer["id"],
        amount_pence=500,
        linked_payment_id=payment_two["id"],
    )
    create_donation(person_id=volunteer["id"], amount_pence=900)

    report = {row["person_id"]: row for row in list_payment_reconciliation()}
    volunteer_report = report[volunteer["id"]]
    assert volunteer_report["payment_total_pence"] == 7050
    assert volunteer_report["linked_donation_total_pence"] == 1500
    assert volunteer_report["difference_pence"] == 5550
    assert volunteer_report["payment_count"] == 2
    assert volunteer_report["linked_donation_count"] == 2
    assert report[other_volunteer["id"]]["difference_pence"] == 1200

    response = signed_in_client().get("/reports/payment-reconciliation")
    assert response.status_code == 200
    assert response.get_json()["items"] == list_payment_reconciliation()


def test_person_reconciliation_counts_multi_linked_donation_only_once():
    """Referencing a donation from two payments does not double its aggregate."""
    person = create_person(name="Multi-payment reconciliation", role="volunteer")
    payment_one = create_payment(person_id=person["id"], amount_pence=2000)
    payment_two = create_payment(person_id=person["id"], amount_pence=3000)
    donation = create_donation(
        person_id=person["id"],
        amount_pence=700,
        linked_payment_ids=[payment_one["id"], payment_two["id"]],
    )

    report = next(
        row for row in list_payment_reconciliation()
        if row["person_id"] == person["id"]
    )
    assert report["payment_total_pence"] == 5000
    assert report["linked_donation_total_pence"] == 700
    assert report["linked_donation_count"] == 1
    assert report["difference_pence"] == 4300
    assert donation["linked_payment_ids"] == [payment_one["id"], payment_two["id"]]


def test_admin_financial_report_aggregates_totals_and_volunteer_activity():
    """Report totals include active club finances and volunteer mileage/payment rows."""
    volunteer = create_person(name="Report Volunteer", role="volunteer")
    other_volunteer = create_person(name="Second Report Volunteer", role="volunteer")
    parent = create_person(name="Non-volunteer donor", role="parent")
    create_trip(
        person_id=volunteer["id"],
        trip_date=date(2026, 5, 1),
        vehicle_type="car",
        miles_one_way=10,
        return_trip=True,
        declaration_text="I declare this trip was for club business.",
    )
    create_trip(
        person_id=volunteer["id"],
        trip_date=date(2026, 5, 2),
        vehicle_type="bicycle",
        miles_one_way=2.5,
        return_trip=False,
        declaration_text="I declare this trip was for club business.",
    )
    create_payment(person_id=volunteer["id"], amount_pence=7500)
    eligible_donation = create_donation(
        person_id=volunteer["id"], amount_pence=4000, gift_aid_eligible=True,
    )
    create_donation(person_id=volunteer["id"], amount_pence=2000)
    dormant_donation = create_donation(
        person_id=volunteer["id"], amount_pence=600, gift_aid_eligible=True,
    )
    db_module.update_donation(dormant_donation["id"], active=False)
    create_donation(person_id=parent["id"], amount_pence=1000, gift_aid_eligible=True)

    client = signed_in_client()
    page = client.get("/reports/page")
    assert page.status_code == 200
    page_html = page.get_data(as_text=True)
    assert 'id="summary-volunteer-filter"' in page_html
    for report_type in ("trips", "donations", "payments"):
        assert f'id="{report_type}-person-filter"' in page_html
        assert f'id="{report_type}-date-from"' in page_html
        assert f'id="{report_type}-date-to"' in page_html
        assert f'data-export-filename="{report_type}"' in page_html
        assert f'id="{report_type}-report-table"' in page_html
    for filter_id in (
        "trips-vehicle-filter", "trips-tax-year-filter", "trips-status-filter",
        "donations-eligibility-filter", "donations-status-filter", "donations-method-filter",
        "payments-donations-filter", "payments-method-filter",
    ):
        assert f'id="{filter_id}"' in page_html
    assert "Export CSV" in page_html
    response = client.get("/reports/financial")
    assert response.status_code == 200
    report = response.get_json()
    assert report["totals"] == {
        "expenses_paid_pence": 7500,
        "donations_pence": 7000,
        "eligible_donations_pence": 5000,
        "gift_aid_pence": 1250,
    }
    volunteer_report = next(
        entry for entry in report["volunteers"]
        if entry["person_id"] == volunteer["id"]
    )
    assert volunteer_report == {
        "person_id": volunteer["id"],
        "person_name": "Report Volunteer",
        "trip_count": 2,
        "total_miles": 22.5,
        "expenses_paid_pence": 7500,
        "donations_pence": 6000,
        "difference_pence": 1500,
    }
    second_report = next(
        entry for entry in report["volunteers"]
        if entry["person_id"] == other_volunteer["id"]
    )
    assert second_report["trip_count"] == 0
    assert second_report["total_miles"] == 0
    assert second_report["difference_pence"] == 0
    assert eligible_donation["active"] == 1


def test_financial_report_is_admin_only():
    """Financial report page and data are restricted to administrator accounts."""
    treasurer = signed_in_client("treasurer")
    volunteer_person = create_person(name="Report access volunteer")
    volunteer = signed_in_client("volunteer", volunteer_person["id"])
    for client in (treasurer, volunteer):
        assert client.get("/reports/page").status_code == 403
        assert client.get("/reports/financial").status_code == 403


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


def test_donation_can_be_edited_made_dormant_and_restored_with_audit():
    """Donation edits, dormancy, restoration, creator history, and access are verified."""
    person = create_person(name="Donation audit volunteer")
    reimbursement = create_payment(person_id=person["id"], amount_pence=5000)
    client = signed_in_client()
    csrf = {"X-CSRF-Token": "test-csrf-token"}
    created = client.post(
        "/donations",
        json={
            "person_id": person["id"],
            "amount_pence": 2500,
            "linked_payment_id": reimbursement["id"],
        },
        headers=csrf,
    )
    assert created.status_code == 201
    donation_id = created.get_json()["id"]
    donation = next(row for row in list_donations() if row["id"] == donation_id)
    assert donation["created_by"] == "test-admin-user"

    updated = client.put(
        f"/donations/{donation_id}",
        json={"amount_pence": 2750},
        headers=csrf,
    )
    assert updated.status_code == 200
    assert updated.get_json()["amount_pence"] == 2750
    assert list_payments(person["id"])[0]["donated_back_pence"] == 2750

    dormant = client.put(
        f"/donations/{donation_id}",
        json={"active": False},
        headers=csrf,
    )
    assert dormant.status_code == 200
    assert dormant.get_json()["active"] == 0
    assert get_summary()["donations_pence"] == 0
    assert list_payments(person["id"])[0]["donated_back_pence"] == 0

    restored = client.put(
        f"/donations/{donation_id}",
        json={"active": True},
        headers=csrf,
    )
    assert restored.status_code == 200
    assert restored.get_json()["active"] == 1
    assert get_summary()["donations_pence"] == 2750

    audit = client.get("/admin/audit")
    assert audit.status_code == 200
    events = [event for event in audit.get_json()["items"] if event["entity_id"] == str(donation_id)]
    assert [event["action"] for event in events] == ["reactivated", "dormant", "updated", "created"]
    assert events[-1]["changed_fields"]
    assert client.get("/admin/audit/page").status_code == 200
    assert signed_in_client("treasurer").get("/admin/audit").status_code == 403


def test_declaration_crud_uses_logical_delete_and_audit():
    """Declarations can be viewed, edited, and made dormant without row removal."""
    person = create_person(name="Declaration claimant")
    client = signed_in_client()
    csrf = {"X-CSRF-Token": "test-csrf-token"}
    created = client.post(
        "/declarations",
        json={
            "person_id": person["id"],
            "full_name": "Declaration claimant",
            "home_address": "12 Example Road",
            "postcode": "AB1 2CD",
            "scope": "future",
        },
        headers=csrf,
    )
    assert created.status_code == 201
    declaration_id = created.get_json()["id"]
    assert created.get_json()["active"] == 1

    fetched = client.get(f"/declarations/{declaration_id}")
    assert fetched.status_code == 200
    assert fetched.get_json()["full_name"] == "Declaration claimant"

    updated = client.put(
        f"/declarations/{declaration_id}",
        json={"full_name": "Updated Claimant", "scope": "past_and_future"},
        headers=csrf,
    )
    assert updated.status_code == 200
    assert updated.get_json()["full_name"] == "Updated Claimant"
    assert updated.get_json()["scope"] == "past_and_future"

    deleted = client.delete(f"/declarations/{declaration_id}", headers=csrf)
    assert deleted.status_code == 200
    assert deleted.get_json() == {"deleted": True, "active": False}
    assert client.get(f"/declarations/{declaration_id}").get_json()["active"] == 0
    assert client.get("/declarations").get_json()["items"][0]["id"] == declaration_id
    assert len(list_declarations(person["id"])) == 1

    restored = client.put(
        f"/declarations/{declaration_id}",
        json={"active": True},
        headers=csrf,
    )
    assert restored.status_code == 200
    assert restored.get_json()["active"] == 1

    events = [
        event for event in client.get("/admin/audit").get_json()["items"]
        if event["entity_type"] == "declaration" and event["entity_id"] == str(declaration_id)
    ]
    assert [event["action"] for event in events] == [
        "reactivated", "dormant", "updated", "created",
    ]


def test_declaration_updates_and_deletes_return_not_found():
    """Declaration mutation endpoints report missing records without fabricating success."""
    client = signed_in_client()
    csrf = {"X-CSRF-Token": "test-csrf-token"}
    assert client.get("/declarations/999").status_code == 404
    assert client.put("/declarations/999", json={"full_name": "Missing"}, headers=csrf).status_code == 404
    assert client.delete("/declarations/999", headers=csrf).status_code == 404


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
        "INSERT INTO gift_aid_declarations (person_id, full_name) VALUES (1, 'Legacy Donor')"
    )
    conn.commit()
    conn.close()

    create_app_database()

    assert db_module.list_declarations()[0]["active"] == 1


def test_auth_blocks_unauthenticated_requests():
    """Unauthenticated API calls fail while browser page visits go to login."""
    client = app.test_client()
    assert client.get("/people").status_code == 401
    page_response = client.get("/destinations/page", headers={"Accept": "text/html"})
    assert page_response.status_code == 302
    assert page_response.location.endswith("/login")


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


def test_login_requires_csrf_and_authenticates_database_user():
    """Login requires CSRF and establishes an authenticated database-backed session."""
    create_user(username="admin-login", password="a-secure-test-password", role="admin")
    client = app.test_client()
    login_page = client.get("/login")
    assert login_page.status_code == 200
    with client.session_transaction() as browser_session:
        token = browser_session["_csrf_token"]

    missing_token = client.post(
        "/login",
        data={"username": "admin-login", "password": "a-secure-test-password"},
    )
    assert missing_token.status_code == 400
    success = client.post(
        "/login",
        data={"username": "admin-login", "password": "a-secure-test-password", "_csrf_token": token},
    )
    assert success.status_code == 302
    assert client.get("/people").status_code == 200


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


def test_trip_csv_template_headers_match_import_permissions():
    """Admins get an assignment column; non-admin templates do not."""
    admin = signed_in_client()
    admin_template = admin.get("/trips/import/template")
    assert admin_template.status_code == 200
    assert admin_template.mimetype == "text/csv"
    assert "attachment;" in admin_template.headers["Content-Disposition"]
    assert admin_template.get_data(as_text=True).strip().split(",") == [
        "person_id", "origin_id", "destination_id", "trip_date", "vehicle_type",
        "return_trip", "passengers", "declaration_accepted",
    ]
    trip_page = admin.get("/trips/page").get_data(as_text=True)
    assert 'href="/trips/import/template"' in trip_page
    assert 'id="trip-import-errors"' in trip_page

    person = create_person(name="Template user")
    volunteer = signed_in_client("volunteer", person["id"])
    volunteer_template = volunteer.get("/trips/import/template")
    assert volunteer_template.status_code == 200
    assert volunteer_template.get_data(as_text=True).strip().split(",") == [
        "origin_id", "destination_id", "trip_date", "vehicle_type",
        "return_trip", "passengers", "declaration_accepted",
    ]
    assert 'href="/trips/import/template"' in volunteer.get("/trips/page").get_data(as_text=True)


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


def test_mutating_api_rejects_missing_or_invalid_csrf_token():
    """State-changing API requests are rejected without the session CSRF token."""
    client = signed_in_client()
    response = client.post("/people", json={"name": "Not created"})
    assert response.status_code == 400
    assert response.get_json()["error"] == "CSRF token missing or invalid."


def test_admin_can_create_users_and_treasurer_cannot_manage_users():
    """Only admins can create or list application user accounts."""
    admin = signed_in_client()
    response = admin.post(
        "/admin/users",
        json={"username": "treasurer", "password": "another-secure-password", "role": "treasurer"},
        headers={"X-CSRF-Token": "test-csrf-token"},
    )
    assert response.status_code == 201
    assert "password_hash" not in response.get_json()
    users = admin.get("/admin/users").get_json()["items"]
    assert next(user for user in users if user["username"] == "treasurer")["role"] == "treasurer"

    treasurer = signed_in_client("treasurer")
    assert treasurer.get("/admin/users").status_code == 403


def test_each_masterfile_has_its_own_page_with_role_checks():
    """Each masterfile page loads for allowed roles and is blocked otherwise."""
    treasurer = signed_in_client("treasurer")
    for path in (
        "/",
        "/people/page",
        "/trips/page",
        "/destinations/page",
        "/declarations/page",
        "/donations/page",
        "/payments/page",
    ):
        response = treasurer.get(path)
        assert response.status_code == 200, path
        assert b"Club Gift Aid Manager" in response.data

    volunteer = create_person(name="Page volunteer", role="volunteer")
    volunteer_user = signed_in_client("volunteer", volunteer["id"])
    assert volunteer_user.get("/trips/page").status_code == 200
    assert volunteer_user.get("/people/page").status_code == 200
    assert volunteer_user.get("/donations/page").status_code == 403

    admin = signed_in_client("admin")
    assert admin.get("/users/page").status_code == 200


def test_pages_render_instructions_and_shared_field_help():
    """Every application page exposes instructions and loads field-help behavior."""
    admin = signed_in_client()
    for path in (
        "/",
        "/claim",
        "/people/page",
        "/trips/page",
        "/destinations/page",
        "/declarations/page",
        "/donations/page",
        "/payments/page",
        "/users/page",
        "/admin/audit/page",
    ):
        response = admin.get(path)
        assert response.status_code == 200
        page = response.get_data(as_text=True)
        assert 'id="page-instructions-modal"' in page
        assert ">Instructions</button>" in page
        assert "js/help.js" in page

        assert 'id="privacy-notice-modal"' in page
        assert "Privacy / GDPR" in page
        assert "js/privacy.js" in page

    login_page = admin.get("/login").get_data(as_text=True)
    assert 'id="page-instructions-modal"' in login_page
    assert "js/help.js" in login_page
    assert 'id="privacy-notice-modal"' in login_page
    assert "Privacy / GDPR" in login_page
    assert "js/privacy.js" in login_page


def test_every_named_template_field_has_specific_help_text():
    """Keep the field-help map aligned with every visible template form control."""
    template_dir = Path(__file__).resolve().parents[1] / "templates"
    help_script = (template_dir.parent / "static" / "js" / "help.js").read_text(encoding="utf-8")
    help_keys = set(re.findall(r"^\s*['\"]?([a-zA-Z0-9_-]+)['\"]?:", help_script, re.MULTILINE))
    field_names = set()
    for template in template_dir.glob("*.html"):
        source = template.read_text(encoding="utf-8")
        field_names.update(re.findall(r'\bname="([^"]+)"', source))

    field_names -= {"_csrf_token", "csrf-token", "viewport", "id"}
    assert field_names <= help_keys
    assert "document.querySelectorAll('input, select, textarea')" in help_script
    assert "new bootstrap.Popover(helpButton)" in help_script


def test_admin_can_replace_privacy_notice_with_text_file(tmp_path, monkeypatch):
    """Admins can replace the fixed text notice, and content remains plain text."""
    notice_path = tmp_path / "privacy-notice.txt"
    notice_path.write_text("Old notice", encoding="utf-8")
    monkeypatch.setattr(app_module, "PRIVACY_NOTICE_PATH", notice_path)
    admin = signed_in_client()

    response = admin.post(
        "/admin/privacy-notice",
        data={"file": (BytesIO(b"Updated <script>alert(1)</script>\nPrivacy info"), "notice.txt")},
        headers={"X-CSRF-Token": "test-csrf-token"},
    )

    assert response.status_code == 200
    assert notice_path.read_text(encoding="utf-8") == "Updated <script>alert(1)</script>\nPrivacy info"
    page = admin.get("/").get_data(as_text=True)
    assert "Updated &lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert any(
        event["entity_type"] == "privacy_notice" and event["action"] == "updated"
        for event in admin.get("/admin/audit").get_json()["items"]
    )


def test_non_admin_cannot_upload_privacy_notice_and_bad_upload_preserves_current_text(tmp_path, monkeypatch):
    """Notice edits require admin permissions and invalid files do not replace the text."""
    notice_path = tmp_path / "privacy-notice.txt"
    notice_path.write_text("Existing privacy notice", encoding="utf-8")
    monkeypatch.setattr(app_module, "PRIVACY_NOTICE_PATH", notice_path)
    treasurer = signed_in_client("treasurer")

    forbidden = treasurer.post(
        "/admin/privacy-notice",
        data={"file": (BytesIO(b"replacement"), "notice.txt")},
        headers={"X-CSRF-Token": "test-csrf-token"},
    )
    assert forbidden.status_code == 403

    invalid = signed_in_client().post(
        "/admin/privacy-notice",
        data={"file": (BytesIO(b"not a text file"), "notice.csv")},
        headers={"X-CSRF-Token": "test-csrf-token"},
    )
    assert invalid.status_code == 400
    assert notice_path.read_text(encoding="utf-8") == "Existing privacy notice"


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


def test_route_distance_api_geocodes_caches_and_returns_road_miles(monkeypatch):
    """First route lookup geocodes both locations, stores coordinates, and routes."""
    origin = create_destination(name="Club Ground", address="Club Road", town_city="London")
    destination = create_destination(name="Away Ground", address="Rugby Lane", town_city="London")
    client = signed_in_client()
    request_urls = []
    responses = iter(
        [
            [{"lat": "51.5", "lon": "-0.1"}],
            [{"lat": "51.6", "lon": "-0.2"}],
            {"code": "Ok", "routes": [{"distance": 16093.44}]},
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
    result = response.get_json()
    assert result["distance_miles"] == 10.0
    assert result["provider"] == "OpenStreetMap / OSRM"
    assert len(request_urls) == 3
    assert db_module.get_destination(origin["id"])["latitude"] == 51.5
    assert db_module.get_destination(destination["id"])["longitude"] == -0.2


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


def test_location_csv_template_has_all_import_headers_and_is_role_gated():
    """Staff can download the location template; volunteers cannot."""
    admin = signed_in_client()
    response = admin.get("/destinations/import/template")
    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    assert "locations-template.csv" in response.headers["Content-Disposition"]
    assert response.get_data(as_text=True).strip().split(",") == [
        "name", "address", "town_city", "postcode", "notes", "home_ground",
    ]
    assert 'href="/destinations/import/template"' in admin.get("/destinations/page").get_data(as_text=True)

    person = create_person(name="Location template volunteer")
    volunteer = signed_in_client("volunteer", person["id"])
    assert volunteer.get("/destinations/import/template").status_code == 403


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
