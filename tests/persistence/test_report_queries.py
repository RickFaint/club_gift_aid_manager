"""Query and decryption budgets for aggregate financial read models."""

import base64
from datetime import date

from sqlalchemy import event

from club_gift_aid_manager import database
from club_gift_aid_manager import db
from club_gift_aid_manager.models import types as encrypted_types
from club_gift_aid_manager.repositories import SQLAlchemyReportRepository


def _capture_report_work(monkeypatch, callback):
    """Return a report with its SQL statement and encrypted-value read counts."""
    statements = []
    decryptions = 0
    original_decrypt = encrypted_types.decrypt_value

    def count_statement(*_args):
        """Capture each SQL statement issued while building the report read model."""
        statements.append(_args[2])

    def count_decryption(value):
        """Count encrypted values materialized before delegating decryption."""
        nonlocal decryptions
        decryptions += 1
        return original_decrypt(value)

    engine = database.get_engine()
    event.listen(engine, "before_cursor_execute", count_statement)
    monkeypatch.setattr(encrypted_types, "decrypt_value", count_decryption)
    try:
        with database.unit_of_work() as session:
            result = callback(SQLAlchemyReportRepository(session))
    finally:
        event.remove(engine, "before_cursor_execute", count_statement)
    return result, statements, decryptions


def test_financial_report_uses_one_projected_metric_stream(monkeypatch):
    """Volunteer names and relevant metrics are the only encrypted values read."""
    volunteer = db.create_person(name="Projected report volunteer", role="volunteer")
    parent = db.create_person(name="Projected report parent", role="parent")
    db.create_trip(
        person_id=volunteer["id"],
        trip_date=date(2026, 6, 1),
        vehicle_type="car",
        miles_one_way=5,
        return_trip=True,
        declaration_text="I declare this trip was for club business.",
    )
    db.create_payment(person_id=volunteer["id"], amount_pence=4000)
    db.create_donation(person_id=volunteer["id"], amount_pence=1500)
    db.create_donation(person_id=parent["id"], amount_pence=500, gift_aid_eligible=True)
    dormant = db.create_donation(person_id=volunteer["id"], amount_pence=900)
    db.update_donation(dormant["id"], active=False)

    report, statements, decryptions = _capture_report_work(
        monkeypatch, lambda repository: repository.financial_report()
    )

    assert len(statements) == 2
    assert sum("UNION ALL" in statement.upper() for statement in statements) == 1
    assert decryptions == 5  # one volunteer name plus four active metric values
    assert report["totals"]["expenses_paid_pence"] == 4000
    assert report["totals"]["donations_pence"] == 2000


def test_complete_financial_report_query_count_is_constant_per_volunteer():
    """Outstanding expense calculation batches trips instead of querying per person."""
    for index in range(4):
        person = db.create_person(name=f"Batch report volunteer {index}", role="volunteer")
        db.create_trip(
            person_id=person["id"],
            trip_date=date(2026, 6, index + 1),
            vehicle_type="car",
            miles_one_way=5,
            return_trip=True,
            declaration_text="I declare this trip was for club business.",
        )

    statements = []
    engine = database.get_engine()

    def capture(*args):
        statements.append(args[2])

    event.listen(engine, "before_cursor_execute", capture)
    try:
        report = db.get_financial_report()
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert len(statements) == 5
    assert len(report["volunteers"]) == 4
    assert report["totals"]["expenses_outstanding_pence"] == 1800


def test_declaration_listing_does_not_load_deferred_evidence():
    """The persisted presence flag avoids an image query and decryption per row."""
    image = base64.b64encode(b"encrypted-at-rest evidence marker").decode("ascii")
    for index in range(3):
        person = db.create_person(name=f"Declaration query donor {index}")
        db.create_declaration(
            person_id=person["id"],
            full_name=person["name"],
            evidence_image_base64=image,
            evidence_image_mime="image/png",
        )

    statements = []
    engine = database.get_engine()

    def capture(*args):
        statements.append(args[2])

    event.listen(engine, "before_cursor_execute", capture)
    try:
        declarations = db.list_declarations()
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert len(statements) == 2
    assert all("evidence_image_base64" not in statement for statement in statements)
    assert all(item["has_evidence_image"] for item in declarations)


def test_summary_counts_and_amounts_use_two_queries(monkeypatch):
    """Headline counts share one query and encrypted amounts share one stream."""
    person = db.create_person(name="Summary projection", role="volunteer")
    db.create_payment(person_id=person["id"], amount_pence=2500)
    db.create_donation(person_id=person["id"], amount_pence=700)

    summary, statements, decryptions = _capture_report_work(
        monkeypatch, lambda repository: repository.summary()
    )

    assert len(statements) == 2
    assert sum("UNION ALL" in statement.upper() for statement in statements) == 1
    assert decryptions == 2
    assert summary["people"] == 1
    assert summary["payments_pence"] == 2500
    assert summary["donations_pence"] == 700


def test_reconciliation_reads_linked_donation_once(monkeypatch):
    """A donation linked to several payments is projected and decrypted once."""
    person = db.create_person(name="Reconciliation projection", role="volunteer")
    first = db.create_payment(person_id=person["id"], amount_pence=2000)
    second = db.create_payment(person_id=person["id"], amount_pence=3000)
    db.create_donation(
        person_id=person["id"],
        amount_pence=800,
        linked_payment_ids=[first["id"], second["id"]],
    )

    rows, statements, decryptions = _capture_report_work(
        monkeypatch, lambda repository: repository.payment_reconciliation()
    )

    assert len(statements) == 2
    assert sum("UNION ALL" in statement.upper() for statement in statements) == 1
    assert decryptions == 4  # one name, two payments, one distinct donation
    assert rows[0]["payment_total_pence"] == 5000
    assert rows[0]["linked_donation_total_pence"] == 800
    assert rows[0]["linked_donation_count"] == 1
