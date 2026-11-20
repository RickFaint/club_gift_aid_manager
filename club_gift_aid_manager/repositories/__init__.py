"""Stable public imports for repository contracts and implementations."""

from .base import SQLAlchemyRepository
from .destinations import SQLAlchemyDestinationRepository
from .finance import (
    SQLAlchemyDeclarationRepository,
    SQLAlchemyDonationPaymentRepository,
    SQLAlchemyDonationRepository,
    SQLAlchemyPaymentRepository,
    SQLAlchemyPaymentTripRepository,
)
from .interfaces import (
    AuditRepository,
    DeclarationRepository,
    DestinationRepository,
    DonationPaymentRepository,
    DonationRepository,
    LoginAttemptRepository,
    MileageRateRepository,
    PaymentRepository,
    PaymentTripRepository,
    PersonRepository,
    PrivacyNoticeRepository,
    Repository,
    TripRepository,
    UserRepository,
)
from .people import SQLAlchemyPersonRepository
from .privacy import SQLAlchemyPrivacyNoticeRepository
from .rates import SQLAlchemyMileageRateRepository
from .reports import SQLAlchemyReportRepository
from .security import SQLAlchemyAuditRepository, SQLAlchemyLoginAttemptRepository
from .trips import SQLAlchemyTripRepository
from .users import SQLAlchemyUserRepository

__all__ = [name for name in globals() if name.endswith("Repository")]
