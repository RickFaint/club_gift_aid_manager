"""Public model exports and complete SQLAlchemy metadata registration.

Importing this package loads every model module.  This is essential before
``Base.metadata.create_all`` so SQLAlchemy knows about the entire schema.
"""

from .audit import AuditEvent
from .auth_session import AuthSession
from .base import Base
from .declaration import GiftAidDeclaration
from .destination import Destination
from .donation import Donation, DonationPayment
from .login_attempt import LoginAttempt
from .mileage_rate import MileageRateSchedule
from .payment import Payment, PaymentTrip
from .person import Person
from .privacy_notice import PrivacyNoticeVersion
from .route_lookup_cache import RouteLookupCache
from .trip import Trip, TripCorrection
from .types import EncryptedText
from .user import User
from .user_mfa import UserMFA

__all__ = [
    "AuditEvent",
    "AuthSession",
    "Base",
    "Destination",
    "Donation",
    "DonationPayment",
    "EncryptedText",
    "GiftAidDeclaration",
    "LoginAttempt",
    "MileageRateSchedule",
    "Payment",
    "PaymentTrip",
    "Person",
    "PrivacyNoticeVersion",
    "RouteLookupCache",
    "Trip",
    "TripCorrection",
    "User",
    "UserMFA",
]
