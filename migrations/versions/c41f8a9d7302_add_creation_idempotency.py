"""Add request tokens for idempotent claim and payment creation."""
import sqlalchemy as sa
from alembic import op

revision = "c41f8a9d7302"
down_revision = "b149ac72e405"
branch_labels = None
depends_on = None

def upgrade() -> None:
    """Add unique request tokens that make claim and payment retries idempotent.

    Nullable unique indexes allow historic rows to remain unchanged while ensuring
    a retried modern request cannot create the same financial record twice.
    """
    op.add_column("trips", sa.Column("request_token", sa.Text(), nullable=True))
    op.create_index("uq_trips_request_token", "trips", ["request_token"], unique=True)
    op.add_column("payments", sa.Column("request_token", sa.Text(), nullable=True))
    op.create_index("uq_payments_request_token", "payments", ["request_token"], unique=True)

def downgrade() -> None:
    """Remove idempotency indexes and token columns in reverse dependency order.

    This rollback restores the earlier schema but also removes duplicate-submission
    protection, so operators should use it only as part of a controlled downgrade.
    """
    op.drop_index("uq_payments_request_token", table_name="payments")
    op.drop_column("payments", "request_token")
    op.drop_index("uq_trips_request_token", table_name="trips")
    op.drop_column("trips", "request_token")
