"""Store reimbursement calculations and correction notes for paid trips."""

import sqlalchemy as sa
from alembic import op


revision = "2026b0719a01"
down_revision = "9c2f17d33a41"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add append-only correction records and snapshots for new payments."""
    op.add_column("payments", sa.Column("calculation_snapshot", sa.Text(), nullable=True))
    op.create_table(
        "trip_corrections",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("trip_id", sa.Integer(), sa.ForeignKey("trips.id"), nullable=False),
        sa.Column("author_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )
    op.create_index("ix_trip_corrections_trip_id", "trip_corrections", ["trip_id"])


def downgrade() -> None:
    """Remove new evidence storage when explicitly reverting the release."""
    op.drop_index("ix_trip_corrections_trip_id", table_name="trip_corrections")
    op.drop_table("trip_corrections")
    op.drop_column("payments", "calculation_snapshot")
