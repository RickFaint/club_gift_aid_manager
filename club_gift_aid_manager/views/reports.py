"""Reports routes.

A Blueprint is Flask's way of collecting related URL handlers.  Defining a
route here does not start a second application: create_app() later registers
this collection on the one Flask application object.
"""

import csv
import json
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile
from datetime import date
from io import StringIO

from flask import Blueprint, Response, request, stream_with_context
from sqlalchemy import select

from ..core.validation import ValidationError, as_date, as_int
from ..database import unit_of_work
from ..repositories import SQLAlchemyReportRepository

from .shared import (
    Role,
    g,
    get_dashboard_checks,
    get_financial_report,
    get_pagination,
    get_person_dashboard,
    get_summary,
    jsonify,
    list_audit_events,
    list_payment_reconciliation,
    paginated_response,
    quote_trip_expenses,
    render_template,
)
from ..auth.decorators import roles_required


blueprint = Blueprint("reports", __name__)

DETAIL_FIELDS = {
    "trips": {"vehicle", "tax-year", "status"},
    "donations": {"eligible", "status", "method"},
    "payments": {"related-donations", "method"},
}
DETAIL_HEADERS = {
    "trips": (
        "Date",
        "Volunteer",
        "Origin",
        "Destination",
        "Tax year",
        "Vehicle",
        "Miles",
        "Expense",
        "Payment",
        "Matching donation received",
        "Status",
    ),
    "donations": (
        "Date",
        "Volunteer",
        "Amount",
        "Received",
        "Method",
        "Payment references",
        "Gift Aid eligible",
        "Status",
    ),
    "payments": (
        "Date",
        "Volunteer",
        "Amount paid",
        "Method",
        "Reference",
        "Trips covered",
        "Related donations (reference only)",
    ),
}

# These columns contain credentials or authentication secrets.  The all-data
# export still includes a CSV for their table, but deliberately omits these
# values so a routine report download cannot become a credential backup.
EXPORT_SECRET_COLUMNS = {
    "password_hash",
    "username_bidx",
    "secret",
    "recovery_code_hashes",
    "evidence_image_base64",
}


@blueprint.get("/reports/page")
@roles_required(Role.ADMIN)
def reports_page():
    """Render the administrator-only financial and volunteer report."""
    return render_template("reports.html")


@blueprint.get("/reports/summary")
@roles_required(Role.ADMIN, Role.TREASURER)
def reports_summary():
    """Return high-level dashboard counters and financial totals."""
    return jsonify(get_summary())


@blueprint.get("/dashboard/overview")
@roles_required(Role.ADMIN, Role.TREASURER, Role.VOLUNTEER)
def dashboard_overview():
    """Return dashboard prompts scoped to the signed-in user's role."""
    if g.current_user["role"] == Role.VOLUNTEER.value:
        return jsonify(get_person_dashboard(g.current_user["person_id"]))

    overview = get_dashboard_checks()
    if g.current_user["role"] == Role.ADMIN.value:
        overview["recent_activity"] = list_audit_events(limit=5)
    return jsonify(overview)


@blueprint.get("/reports/payment-reconciliation")
@roles_required(Role.ADMIN, Role.TREASURER)
def reports_payment_reconciliation():
    """Return per-person payments minus donations explicitly linked to payments."""
    return jsonify({"items": list_payment_reconciliation()})


@blueprint.get("/reports/financial")
@roles_required(Role.ADMIN)
def reports_financial():
    """Return club totals and per-volunteer trip and finance aggregates."""
    return jsonify(get_financial_report())


@blueprint.get("/reports/detail-options")
@roles_required(Role.ADMIN)
def reports_detail_options():
    """Return detail filter values for the full datasets."""
    with unit_of_work() as database:
        return jsonify(SQLAlchemyReportRepository(database).detail_options())


def _detail_filters(section: str) -> dict:
    """Parse the shared filters for paginated report reads and CSV exports."""
    filters = {}
    for field in DETAIL_FIELDS[section] | {"person", "from", "to"}:
        value = request.args.get(field, "")
        if not value:
            continue
        if field in {"from", "to"}:
            filters[field] = as_date(value, field).isoformat()
        elif field == "person":
            identifier = as_int(value, "person")
            if not 0 < identifier <= 2**63 - 1:
                raise ValidationError("Select a valid person.")
            filters[field] = identifier
        else:
            if len(value) > 100:
                raise ValidationError(f"The {field} filter is too long.")
            if field in {"related-donations", "eligible"} and value not in {"yes", "no"}:
                raise ValidationError(f"Select a valid {field} filter.")
            if section == "donations" and field == "status" and value not in {"active", "dormant"}:
                raise ValidationError("Select a valid donation status.")
            filters[field] = value
    if "from" in filters and "to" in filters and filters["from"] > filters["to"]:
        raise ValidationError("The start date must be on or before the end date.")
    return filters


@blueprint.get("/reports/data/<section>")
@roles_required(Role.ADMIN)
def reports_detail(section: str):
    """Page through all records that match a report section and its filters."""
    if section not in DETAIL_FIELDS:
        return jsonify({"error": "Report not found."}), 404
    filters = _detail_filters(section)
    limit, offset = get_pagination()
    items = []
    total = 0
    with unit_of_work() as database:
        for record in SQLAlchemyReportRepository(database).detail_rows(section, filters):
            if offset <= total < offset + limit:
                items.append(record)
            total += 1
    if section == "trips":
        _add_trip_expenses(items)
    return jsonify(paginated_response(items, total, limit, offset))


def _add_trip_expenses(rows: list[dict]) -> None:
    """Attach annual-band expense amounts without changing payment evidence.

    Rows are grouped by volunteer so the shared calculator can account for all
    of that person's trips in the tax year. A paid calculation snapshot wins
    over a fresh quote because it is the immutable evidence saved at payment.
    """
    trip_ids_by_person: dict[int, list[int]] = {}
    for row in rows:
        trip_ids_by_person.setdefault(row["person_id"], []).append(row["id"])
    calculated: dict[int, int] = {}
    for person_id, trip_ids in trip_ids_by_person.items():
        quote = quote_trip_expenses(person_id, trip_ids)
        calculated.update(
            (trip["trip_id"], int(trip["total_amount_pence"])) for trip in quote["trips"]
        )
    for row in rows:
        row["expense_amount_pence"] = (
            row["paid_expense_amount_pence"]
            if row["paid_expense_amount_pence"] is not None
            else calculated[row["id"]]
        )


def _csv_row(section: str, row: dict) -> tuple:
    """Format report values consistently with the detail tables."""
    if section == "trips":
        return (
            row["trip_date"],
            row["person_name"],
            row["origin_name"],
            row["destination_name"],
            row["tax_year"],
            row["vehicle_type"],
            row["total_miles"],
            _pounds(row["expense_amount_pence"]),
            f"Paid (Payment #{row['payment_id']})" if row["payment_id"] else "Unpaid",
            (
                "Yes"
                if row["matching_donation_received"]
                else "Linked, awaiting receipt"
                if row["matching_donation_linked"]
                else "No"
            ),
            row["status"],
        )
    amount = _pounds(row["amount_pence"])
    if section == "donations":
        return (
            row["donated_date"],
            row["person_name"],
            amount,
            row["received_date"],
            row["method"],
            ", ".join(f"Payment #{value}" for value in row["linked_payment_ids"]),
            "Yes" if row["gift_aid_eligible"] else "No",
            "Active" if row["active"] else "Dormant",
        )
    trips = "; ".join(
        f"Trip #{trip['id']} \u00b7 {trip['trip_date']}" for trip in row["linked_trips"]
    )
    return (
        row["paid_date"],
        row["person_name"],
        amount,
        row["method"],
        row["reference"],
        trips,
        _pounds(row["related_donation_amount_pence"]),
    )


def _pounds(pence: int) -> str:
    """Convert integer pence to decimal pounds without a float conversion."""
    whole, fraction = divmod(abs(pence), 100)
    return f"{'-' if pence < 0 else ''}{whole}.{fraction:02}"


def _csv_cell(value):
    """Prevent spreadsheet applications from evaluating text as a formula."""
    if isinstance(value, str):
        if value.startswith(("\t", "\r", "\n")) or value.lstrip().startswith(("=", "+", "-", "@")):
            return "'" + value
    return value


@blueprint.get("/reports/data/<section>/csv")
@roles_required(Role.ADMIN)
def reports_detail_csv(section: str):
    """Download all matching records independently of the displayed report page."""
    if section not in DETAIL_FIELDS:
        return jsonify({"error": "Report not found."}), 404
    filters = _detail_filters(section)

    def rows():
        """Stream an escaped, UTF-8 CSV without retaining the report in memory."""
        buffer = StringIO(newline="")
        writer = csv.writer(buffer)
        yield "\ufeff"
        writer.writerow(DETAIL_HEADERS[section])
        yield buffer.getvalue()
        with unit_of_work() as database:
            records = list(SQLAlchemyReportRepository(database).detail_rows(section, filters))
            if section == "trips":
                _add_trip_expenses(records)
            for record in records:
                buffer.seek(0)
                buffer.truncate()
                writer.writerow(_csv_cell(value) for value in _csv_row(section, record))
                yield buffer.getvalue()

    return Response(
        stream_with_context(rows()),
        mimetype="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{section}-{date.today().isoformat()}.csv"'
        },
    )


def _csv_value(value):
    """Convert ORM values into stable CSV text without evaluating formulas."""
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, sort_keys=True, default=str)
    return _csv_cell(str(value))


def _model_csv(database, model) -> tuple[str, str, int]:
    """Export one mapped SQLAlchemy table, excluding credential material.

    Selecting ORM models (rather than raw SQLite rows) is intentional: encrypted
    ``EncryptedText`` columns are transparently decrypted by SQLAlchemy for this
    authorized administrator export.  Relationship attributes are not columns,
    so only scalar table fields are written.  Secret columns are omitted and
    listed in the manifest created by ``reports_all_csv``.
    """
    columns = [column.name for column in model.__table__.columns]
    exported = [name for name in columns if name not in EXPORT_SECRET_COLUMNS]
    rows = database.scalars(select(model))
    output = StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(exported)
    count = 0
    for row in rows:
        writer.writerow(_csv_value(getattr(row, name, None)) for name in exported)
        count += 1
    return model.__tablename__, output.getvalue(), count


@blueprint.get("/reports/all-csv")
@roles_required(Role.ADMIN)
def reports_all_csv():
    """Download all application tables and report datasets in one ZIP archive.

    The archive contains one CSV per mapped table plus the three complete,
    unpaginated report sections and a financial-summary CSV.  It is restricted
    to administrators because it contains the club's full data set.  Credential
    hashes, MFA secrets, recovery hashes, and large declaration-image payloads
    are omitted from table files and documented in ``MANIFEST.csv``.
    """
    archive = BytesIO()
    manifest_rows = [("file", "rows", "omitted_columns")]
    with ZipFile(archive, "w", ZIP_DEFLATED) as bundle:
        from ..models import Base

        with unit_of_work() as database:
            for mapper in sorted(Base.registry.mappers, key=lambda item: item.local_table.name):
                table_name, contents, count = _model_csv(database, mapper.class_)
                omitted = sorted(
                    column.name
                    for column in mapper.class_.__table__.columns
                    if column.name in EXPORT_SECRET_COLUMNS
                )
                filename = f"tables/{table_name}.csv"
                bundle.writestr(filename, contents)
                manifest_rows.append((filename, count, ", ".join(omitted)))

            for section in DETAIL_FIELDS:
                records = list(SQLAlchemyReportRepository(database).detail_rows(section, {}))
                if section == "trips":
                    _add_trip_expenses(records)
                output = StringIO(newline="")
                writer = csv.writer(output)
                writer.writerow(DETAIL_HEADERS[section])
                for record in records:
                    writer.writerow(_csv_cell(value) for value in _csv_row(section, record))
                filename = f"reports/{section}.csv"
                bundle.writestr(filename, output.getvalue())
                manifest_rows.append((filename, len(records), ""))

        financial = get_financial_report()
        output = StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(("section", "field", "value"))
        for field, value in financial["totals"].items():
            writer.writerow(("totals", field, _csv_value(value)))
        for volunteer in financial["volunteers"]:
            for field, value in volunteer.items():
                writer.writerow((f"volunteer:{volunteer['person_id']}", field, _csv_value(value)))
        bundle.writestr("reports/financial-summary.csv", output.getvalue())
        manifest_rows.append(("reports/financial-summary.csv", len(financial["volunteers"]), ""))

        output = StringIO(newline="")
        csv.writer(output).writerows(manifest_rows)
        bundle.writestr("MANIFEST.csv", output.getvalue())

    return Response(
        archive.getvalue(),
        mimetype="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="club-gift-aid-export-{date.today().isoformat()}.zip"'
        },
    )
