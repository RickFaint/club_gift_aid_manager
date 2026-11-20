"""Add a shared cache for completed route lookups."""

import sqlalchemy as sa
from alembic import op

import club_gift_aid_manager.models.types


revision = "b149ac72e405"
down_revision = "6e8d29f4c713"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the persistent cache used to share completed route calculations.

    Fingerprints bind each cached distance to the location data that produced it,
    while the expiry index supports efficient removal of stale entries.
    """
    op.create_table(
        "route_lookup_cache",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("origin_id", sa.Integer(), nullable=False),
        sa.Column("destination_id", sa.Integer(), nullable=False),
        sa.Column("origin_fingerprint", sa.Text(), nullable=False),
        sa.Column("destination_fingerprint", sa.Text(), nullable=False),
        sa.Column("distance_miles", club_gift_aid_manager.models.types.EncryptedText(), nullable=False),
        sa.Column("expires_at", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["origin_id"], ["destinations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["destination_id"], ["destinations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_index("ix_route_lookup_cache_expires_at", "route_lookup_cache", ["expires_at"])


def downgrade() -> None:
    """Remove the route-cache index and table during an explicit schema rollback.

    Cached distances are derived data, so dropping them does not remove the source
    locations or mileage claims from which application records are built.
    """
    op.drop_index("ix_route_lookup_cache_expires_at", table_name="route_lookup_cache")
    op.drop_table("route_lookup_cache")
