"""Historic mileage reimbursement schedule model."""

from sqlalchemy import CheckConstraint, Integer, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class MileageRateSchedule(Base):
    """Map an effective-dated set of mileage and passenger rates in pence."""

    __tablename__ = "mileage_rate_schedules"
    __table_args__ = tuple(
        CheckConstraint(f"{field} >= 0")
        for field in (
            "car_first_10000_pence",
            "car_over_10000_pence",
            "motorcycle_pence",
            "bicycle_pence",
            "passenger_pence",
        )
    )

    effective_from: Mapped[str] = mapped_column(Text, primary_key=True)
    car_first_10000_pence: Mapped[int] = mapped_column(Integer, nullable=False)
    car_over_10000_pence: Mapped[int] = mapped_column(Integer, nullable=False)
    motorcycle_pence: Mapped[int] = mapped_column(Integer, nullable=False)
    bicycle_pence: Mapped[int] = mapped_column(Integer, nullable=False)
    passenger_pence: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
