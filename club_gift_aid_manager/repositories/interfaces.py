"""Abstract repository contracts used by application services."""

from abc import ABC, abstractmethod
from typing import Generic, TypeVar

from ..models import (
    AuditEvent,
    Base,
    Destination,
    Donation,
    DonationPayment,
    GiftAidDeclaration,
    LoginAttempt,
    MileageRateSchedule,
    Payment,
    PaymentTrip,
    Person,
    PrivacyNoticeVersion,
    Trip,
    User,
)

ModelT = TypeVar("ModelT", bound=Base)
KeyT = TypeVar("KeyT")


class Repository(Generic[ModelT, KeyT], ABC):
    """Operations every persistent aggregate must expose."""

    @abstractmethod
    def get(self, identity: KeyT) -> ModelT | None:
        """Return one model by primary key, or `None` when it does not exist."""
        ...

    @abstractmethod
    def list(self, *, limit: int | None = None, offset: int = 0) -> list[ModelT]:
        """Return a deterministic page of models from the repository."""
        ...

    @abstractmethod
    def count(self) -> int:
        """Return the number of models represented by this repository."""
        ...

    @abstractmethod
    def add(self, model: ModelT) -> ModelT:
        """Stage and flush a new model so generated fields are available."""
        ...

    @abstractmethod
    def delete(self, model: ModelT) -> None:
        """Stage and flush deletion of the supplied persistent model."""
        ...


class PersonRepository(Repository[Person, int], ABC):
    """Define persistence operations for people."""


class UserRepository(Repository[User, int], ABC):
    """Define account persistence plus blind-index username lookup."""

    @abstractmethod
    def by_username_index(self, username_bidx: str) -> User | None:
        """Find an account using the keyed index of its normalized username."""
        ...


class PrivacyNoticeRepository(Repository[PrivacyNoticeVersion, int], ABC):
    """Define persistence operations for immutable privacy notices."""

    @abstractmethod
    def current(self) -> PrivacyNoticeVersion | None:
        """Return the latest published notice, or `None` before initialization."""
        ...


class DeclarationRepository(Repository[GiftAidDeclaration, int], ABC):
    """Define persistence operations for Gift Aid declarations."""


class DestinationRepository(Repository[Destination, int], ABC):
    """Define persistence operations for saved journey locations."""


class TripRepository(Repository[Trip, int], ABC):
    """Define persistence operations for mileage claims."""


class DonationRepository(Repository[Donation, int], ABC):
    """Define persistence operations for donations."""


class PaymentRepository(Repository[Payment, int], ABC):
    """Define persistence operations for reimbursement payments."""


class MileageRateRepository(Repository[MileageRateSchedule, str], ABC):
    """Define persistence operations for effective-dated mileage rates."""


class AuditRepository(Repository[AuditEvent, int], ABC):
    """Define persistence operations for append-only audit events."""


class LoginAttemptRepository(Repository[LoginAttempt, str], ABC):
    """Define persistence operations for login-throttling counters."""


class DonationPaymentRepository(Repository[DonationPayment, tuple[int, int]], ABC):
    """Define persistence operations for donation/payment associations."""


class PaymentTripRepository(Repository[PaymentTrip, tuple[int, int]], ABC):
    """Define persistence operations for payment/trip associations."""
