"""SQLAlchemy repositories for declarations, donations, and payments."""

from ..models import Donation, DonationPayment, GiftAidDeclaration, Payment, PaymentTrip
from .base import SQLAlchemyRepository
from .interfaces import (
    DeclarationRepository,
    DonationPaymentRepository,
    DonationRepository,
    PaymentRepository,
    PaymentTripRepository,
)


class SQLAlchemyDeclarationRepository(
    SQLAlchemyRepository[GiftAidDeclaration, int], DeclarationRepository
):
    """Provide SQLAlchemy persistence for declaration evidence."""

    model_type = GiftAidDeclaration


class SQLAlchemyDonationRepository(SQLAlchemyRepository[Donation, int], DonationRepository):
    """Provide SQLAlchemy persistence for donation records."""

    model_type = Donation


class SQLAlchemyPaymentRepository(SQLAlchemyRepository[Payment, int], PaymentRepository):
    """Provide SQLAlchemy persistence for reimbursement payments."""

    model_type = Payment


class SQLAlchemyDonationPaymentRepository(
    SQLAlchemyRepository[DonationPayment, tuple[int, int]], DonationPaymentRepository
):
    """Persist reference-only donation/payment association rows."""

    model_type = DonationPayment


class SQLAlchemyPaymentTripRepository(
    SQLAlchemyRepository[PaymentTrip, tuple[int, int]], PaymentTripRepository
):
    """Persist the association assigning complete trips to payments."""

    model_type = PaymentTrip
