"""Gift Aid declarations, donations, payments, reconciliation, and financial reports.

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

def test_car_claim_requires_declaration():
    """A claim cannot be submitted without accepted declaration wording."""
    with pytest.raises(ValueError, match="Declaration must be accepted"):
        validate_declaration(False, "I declare that I have taken other parents' children on this journey.")



def test_declaration_snapshot_is_not_changed_by_later_config_updates():
    """Changing current declaration text does not mutate an earlier snapshot."""
    original = get_declaration_text()
    snapshot = original
    update_declaration_text("Updated declaration wording")
    assert snapshot == original
    assert get_declaration_text() == "Updated declaration wording"



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



def test_gift_aid_declaration_records_current_privacy_notice_proof():
    """A declaration retains the notice ID and digest that were current at creation."""
    person = create_person(name="Notice evidence donor")
    shown = db_module.publish_privacy_notice("Notice shown for this declaration")
    declaration = create_declaration(person_id=person["id"], full_name=person["name"])
    db_module.publish_privacy_notice("A later notice")

    assert declaration["privacy_notice_version_id"] == shown["id"]
    assert declaration["privacy_notice_sha256"] == shown["text_sha256"]
    stored = db_module.get_declaration(declaration["id"])
    assert stored["privacy_notice_version_id"] == shown["id"]
    assert stored["privacy_notice_sha256"] == shown["text_sha256"]



def test_gift_aid_declaration_records_the_notice_actually_shown():
    """A concurrently published notice cannot replace the version captured by the form."""
    person = create_person(name="Form notice evidence donor")
    shown = db_module.publish_privacy_notice("Notice rendered with the form")
    client = signed_in_client()
    page = client.get("/declarations/page")
    assert f'name="privacy_notice_version_id" value="{shown["id"]}"' in page.get_data(as_text=True)
    db_module.publish_privacy_notice("Published after the form was rendered")

    response = client.post(
        "/declarations",
        json={
            "person_id": person["id"],
            "full_name": person["name"],
            "privacy_notice_version_id": shown["id"],
            "privacy_notice_sha256": shown["text_sha256"],
        },
        headers={"X-CSRF-Token": "test-csrf-token"},
    )

    assert response.status_code == 201
    declaration = response.get_json()
    assert declaration["privacy_notice_version_id"] == shown["id"]
    assert declaration["privacy_notice_sha256"] == shown["text_sha256"]

