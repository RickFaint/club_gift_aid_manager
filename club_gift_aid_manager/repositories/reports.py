"""Read-model repository for dashboards, reconciliation, and financial reports."""

from collections import defaultdict
from collections.abc import Iterator
import json
from typing import Any

from sqlalchemy import func, literal, or_, select, union_all
from sqlalchemy.orm import Session, load_only, selectinload

from ..domain.roles import Role
from ..models import (
    Destination,
    Donation,
    DonationPayment,
    GiftAidDeclaration,
    Payment,
    PaymentTrip,
    Person,
    Trip,
)


class SQLAlchemyReportRepository:
    """Build aggregate read models without exposing persistence details to Flask."""

    def __init__(self, session: Session) -> None:
        """Bind all projections to the caller's unit-of-work session."""
        self.session = session

    def payment_reconciliation(self) -> list[dict[str, Any]]:
        """Aggregate payments and reference-linked active donations by person."""
        people = dict(
            self.session.execute(
                select(Person.id, Person.name).where(
                    Person.id.in_(select(Payment.person_id)),
                )
            )
            .tuples()
            .all()
        )
        payment_totals: dict[int, dict[str, int]] = {}
        donation_totals: dict[int, dict[str, int]] = {}
        rows = union_all(
            select(
                Payment.person_id.label("person_id"),
                literal("payment").label("kind"),
                Payment.amount_pence.label("amount"),
            ),
            select(
                Donation.person_id.label("person_id"),
                literal("donation").label("kind"),
                Donation.amount_pence.label("amount"),
            ).where(
                Donation.active == 1,
                Donation.id.in_(select(DonationPayment.donation_id)),
            ),
        ).subquery()
        for person_id, kind, amount in self.session.execute(
            select(rows.c.person_id, rows.c.kind, rows.c.amount)
        ):
            target = payment_totals if kind == "payment" else donation_totals
            totals = target.setdefault(person_id, {"total": 0, "count": 0})
            totals["total"] += int(amount)
            totals["count"] += 1
        result = []
        for person_id, payment in payment_totals.items():
            donation = donation_totals.get(person_id, {"total": 0, "count": 0})
            result.append(
                {
                    "person_id": person_id,
                    "person_name": people[person_id],
                    "payment_total_pence": payment["total"],
                    "payment_count": payment["count"],
                    "linked_donation_total_pence": donation["total"],
                    "linked_donation_count": donation["count"],
                    "difference_pence": payment["total"] - donation["total"],
                }
            )
        return sorted(result, key=lambda row: row["person_name"].casefold())

    def summary(self) -> dict[str, int]:
        """Return headline row counts and active financial totals."""
        people, trips = self.session.execute(
            select(
                select(func.count()).select_from(Person).scalar_subquery(),
                select(func.count()).select_from(Trip).scalar_subquery(),
            )
        ).one()
        rows = union_all(
            select(
                literal("donation").label("kind"),
                Donation.amount_pence.label("amount"),
            ).where(Donation.active == 1),
            select(
                literal("payment").label("kind"),
                Payment.amount_pence.label("amount"),
            ),
        ).subquery()
        totals = {"donation": 0, "payment": 0}
        for kind, amount in self.session.execute(select(rows.c.kind, rows.c.amount)):
            totals[kind] += int(amount)
        return {
            "people": int(people or 0),
            "trips": int(trips or 0),
            "donations_pence": totals["donation"],
            "payments_pence": totals["payment"],
        }

    def dashboard_checks(self) -> dict[str, int | bool]:
        """Return staff data-quality checks without making approval decisions."""
        unpaid = int(
            self.session.scalar(
                select(func.count())
                .select_from(Trip)
                .where(
                    ~Trip.id.in_(select(PaymentTrip.trip_id)),
                )
            )
            or 0
        )
        missing_declaration = int(
            self.session.scalar(
                select(func.count())
                .select_from(Donation)
                .outerjoin(
                    GiftAidDeclaration,
                    GiftAidDeclaration.id == Donation.declaration_id,
                )
                .where(
                    Donation.active == 1,
                    Donation.gift_aid_eligible == 1,
                    or_(
                        GiftAidDeclaration.id.is_(None),
                        GiftAidDeclaration.active == 0,
                        GiftAidDeclaration.withdrawn_date.is_not(None),
                    ),
                )
            )
            or 0
        )
        home_ground = self.session.scalar(
            select(Destination.id)
            .where(
                Destination.active == 1,
                Destination.home_ground == 1,
            )
            .limit(1)
        )
        return {
            "trips_without_payment": unpaid,
            "eligible_donations_without_active_declaration": missing_declaration,
            "has_active_home_ground": home_ground is not None,
        }

    def person_dashboard(self, person_id: int | None) -> dict[str, Any]:
        """Return only the selected person's trip count and three recent trips."""
        if person_id is None:
            return {"trip_count": 0, "recent_trips": []}
        count = int(
            self.session.scalar(
                select(func.count())
                .select_from(Trip)
                .where(
                    Trip.person_id == person_id,
                )
            )
            or 0
        )
        models = self.session.scalars(
            select(Trip)
            .where(
                Trip.person_id == person_id,
            )
            .options(selectinload(Trip.destination))
            .order_by(
                Trip.trip_date.desc(),
                Trip.id.desc(),
            )
            .limit(3)
        )
        return {
            "trip_count": count,
            "recent_trips": [
                {
                    "trip_date": trip.trip_date,
                    "status": trip.status,
                    "destination_name": trip.destination.name if trip.destination else None,
                }
                for trip in models
            ],
        }

    def financial_report(self) -> dict[str, Any]:
        """Return club totals and per-volunteer mileage and money aggregates."""
        # Coaches perform volunteer work and can submit the same trips and
        # expenses, even though their directory type remains useful context.
        people = self.session.execute(
            select(Person.id, Person.name, Person.role).where(
                Person.role.in_((Role.VOLUNTEER.value, "coach"))
            )
        )
        trip_count: dict[int, int] = defaultdict(int)
        mileage: dict[int, float] = defaultdict(float)
        paid: dict[int, int] = defaultdict(int)
        donated: dict[int, int] = defaultdict(int)
        eligible = 0
        rows = union_all(
            select(
                Trip.person_id.label("person_id"),
                literal("trip").label("kind"),
                Trip.total_miles.label("value"),
                literal(0).label("eligible"),
            ),
            select(
                Payment.person_id.label("person_id"),
                literal("payment").label("kind"),
                Payment.amount_pence.label("value"),
                literal(0).label("eligible"),
            ),
            select(
                Donation.person_id.label("person_id"),
                literal("donation").label("kind"),
                Donation.amount_pence.label("value"),
                Donation.gift_aid_eligible.label("eligible"),
            ).where(Donation.active == 1),
        ).subquery()
        for person_id, kind, value, is_eligible in self.session.execute(
            select(rows.c.person_id, rows.c.kind, rows.c.value, rows.c.eligible)
        ):
            if kind == "trip":
                trip_count[person_id] += 1
                mileage[person_id] += float(value)
            elif kind == "payment":
                paid[person_id] += int(value)
            else:
                donated[person_id] += int(value)
                if is_eligible:
                    eligible += int(value)
        volunteers = [
            {
                "person_id": person_id,
                "person_name": name,
                "person_type": person_type or Role.VOLUNTEER.value,
                "trip_count": trip_count.get(person_id, 0),
                "total_miles": mileage.get(person_id, 0.0),
                "expenses_paid_pence": paid.get(person_id, 0),
                "donations_pence": donated.get(person_id, 0),
                "difference_pence": paid.get(person_id, 0) - donated.get(person_id, 0),
            }
            for person_id, name, person_type in people
        ]
        volunteers.sort(key=lambda row: (row["person_name"].casefold(), row["person_id"]))
        return {
            "totals": {
                "expenses_paid_pence": sum(paid.values()),
                "donations_pence": sum(donated.values()),
                "eligible_donations_pence": eligible,
                "gift_aid_pence": (eligible * 25 + 50) // 100,
            },
            "volunteers": volunteers,
        }

    def detail_options(self) -> dict[str, list[str]]:
        """Return filter choices across the complete report datasets."""
        return {
            "tax_years": sorted(self.session.scalars(select(Trip.tax_year).distinct())),
            "donation_methods": self._methods(Donation.method),
            "payment_methods": self._methods(Payment.method),
        }

    def _methods(self, column) -> list[str]:
        """Read and deduplicate only the encrypted method column."""
        return sorted(
            {value for value in self.session.scalars(select(column)) if value}, key=str.casefold
        )

    def detail_rows(self, section: str, filters: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """Yield matching report records, projecting only the fields used by detail tables."""
        if section == "trips":
            model, day = Trip, Trip.trip_date
            statement = select(Trip, Person.name).options(
                load_only(
                    Trip.person_id,
                    Trip.trip_date,
                    Trip.tax_year,
                    Trip.total_miles,
                    Trip.vehicle_type,
                    Trip.status,
                ),
                selectinload(Trip.origin).load_only(Destination.name),
                selectinload(Trip.destination).load_only(Destination.name),
                selectinload(Trip.payment_links)
                .selectinload(PaymentTrip.payment)
                .options(
                    load_only(
                        Payment.id,
                        Payment.paid_date,
                        Payment.calculation_snapshot,
                    ),
                    selectinload(Payment.donation_links)
                    .selectinload(DonationPayment.donation)
                    .load_only(
                        Donation.amount_pence,
                        Donation.active,
                        Donation.received_date,
                    ),
                ),
            )
            if filters.get("tax-year"):
                statement = statement.where(Trip.tax_year == filters["tax-year"])
            if filters.get("status") == "paid":
                statement = statement.where(Trip.payment_links.any())
            elif filters.get("status"):
                statement = statement.where(
                    Trip.status == filters["status"], ~Trip.payment_links.any()
                )
        elif section == "donations":
            model, day = Donation, Donation.donated_date
            statement = select(Donation, Person.name).options(
                load_only(
                    Donation.person_id,
                    Donation.donated_date,
                    Donation.amount_pence,
                    Donation.received_date,
                    Donation.method,
                    Donation.active,
                    Donation.gift_aid_eligible,
                ),
                selectinload(Donation.payment_links),
            )
            if filters.get("status"):
                statement = statement.where(Donation.active == int(filters["status"] == "active"))
            if filters.get("eligible"):
                statement = statement.where(
                    Donation.gift_aid_eligible == int(filters["eligible"] == "yes")
                )
        else:
            model, day = Payment, Payment.paid_date
            statement = select(Payment, Person.name).options(
                load_only(
                    Payment.person_id,
                    Payment.paid_date,
                    Payment.amount_pence,
                    Payment.method,
                    Payment.reference,
                ),
                selectinload(Payment.trip_links)
                .selectinload(PaymentTrip.trip)
                .load_only(Trip.trip_date),
                selectinload(Payment.donation_links)
                .selectinload(DonationPayment.donation)
                .load_only(Donation.amount_pence, Donation.active),
            )
            if filters.get("related-donations"):
                has_related = Payment.donation_links.any(
                    DonationPayment.donation.has(Donation.active == 1)
                )
                statement = statement.where(
                    has_related if filters["related-donations"] == "yes" else ~has_related
                )
        statement = statement.join(Person, Person.id == model.person_id)
        if filters.get("person"):
            statement = statement.where(model.person_id == filters["person"])
        if filters.get("from"):
            statement = statement.where(day >= filters["from"])
        if filters.get("to"):
            statement = statement.where(day <= filters["to"])

        for item, name in self.session.execute(
            statement.order_by(day.desc(), model.id.desc()).execution_options(yield_per=200)
        ):
            common = {"id": item.id, "person_id": item.person_id, "person_name": name}
            if section == "trips":
                if filters.get("vehicle") and item.vehicle_type != filters["vehicle"]:
                    continue
                payment = item.payment_links[0].payment if item.payment_links else None
                active_donations = (
                    [link.donation for link in payment.donation_links if link.donation.active]
                    if payment
                    else []
                )
                received_donations = [
                    donation for donation in active_donations if donation.received_date
                ]
                paid_expense = None
                if payment and payment.calculation_snapshot:
                    snapshot = json.loads(payment.calculation_snapshot)
                    evidence = next(
                        (
                            trip
                            for trip in snapshot.get("trips", [])
                            if trip.get("trip_id") == item.id
                        ),
                        None,
                    )
                    if evidence is not None:
                        paid_expense = int(evidence["total_amount_pence"])
                yield common | {
                    "trip_date": item.trip_date,
                    "origin_name": item.origin.name if item.origin else None,
                    "destination_name": item.destination.name if item.destination else None,
                    "tax_year": item.tax_year,
                    "vehicle_type": item.vehicle_type,
                    "total_miles": float(item.total_miles),
                    "status": "paid" if item.payment_links else item.status,
                    "payment_id": payment.id if payment else None,
                    "paid_date": payment.paid_date if payment else None,
                    "paid_expense_amount_pence": paid_expense,
                    "matching_donation_linked": bool(active_donations),
                    "matching_donation_received": bool(received_donations),
                    "matching_donation_amount_pence": sum(
                        int(donation.amount_pence) for donation in received_donations
                    ),
                }
                continue
            if filters.get("method") and item.method != filters["method"]:
                continue
            common |= {"method": item.method, "amount_pence": int(item.amount_pence)}
            if section == "donations":
                yield common | {
                    "donated_date": item.donated_date,
                    "received_date": item.received_date,
                    "gift_aid_eligible": bool(item.gift_aid_eligible),
                    "active": bool(item.active),
                    "linked_payment_ids": sorted(link.payment_id for link in item.payment_links),
                }
            else:
                active_donations = [
                    link.donation for link in item.donation_links if link.donation.active
                ]
                yield common | {
                    "paid_date": item.paid_date,
                    "reference": item.reference,
                    "linked_trips": [
                        {"id": link.trip_id, "trip_date": link.trip.trip_date}
                        for link in sorted(
                            item.trip_links, key=lambda link: (link.trip.trip_date, link.trip_id)
                        )
                    ],
                    "linked_donation_count": len(active_donations),
                    "related_donation_amount_pence": sum(
                        int(donation.amount_pence) for donation in active_donations
                    ),
                }
