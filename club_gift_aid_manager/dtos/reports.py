"""Typed report and dashboard projection DTOs."""

from pydantic import ConfigDict, Field

from .base import DTO


class ReconciliationDTO(DTO):
    """Represent one person's payment-versus-donation reconciliation totals."""

    person_id: int
    person_name: str
    payment_total_pence: int
    donation_total_pence: int
    items: list[dict] = Field(default_factory=list)


class DashboardDTO(DTO):
    """Allow the role-dependent dashboard projection to contain its relevant fields."""

    model_config = ConfigDict(from_attributes=True, extra="allow")


class FinancialReportDTO(DTO):
    """Allow the composite financial report projection returned to administrators."""

    model_config = ConfigDict(from_attributes=True, extra="allow")
