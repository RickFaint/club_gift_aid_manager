"""Add a lightweight declaration-evidence presence flag."""

import sqlalchemy as sa
from alembic import op

revision = "e84f18a2b903"
down_revision = "d72a6c41e905"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add and backfill the flag without reading encrypted payloads in Python."""
    op.add_column(
        "gift_aid_declarations",
        sa.Column("has_evidence_image", sa.Integer(), nullable=False, server_default="0"),
    )
    op.execute(
        "UPDATE gift_aid_declarations SET has_evidence_image = 1 "
        "WHERE evidence_image_base64 IS NOT NULL AND evidence_image_base64 <> ''"
    )


def downgrade() -> None:
    """Remove the derived evidence-presence flag."""
    op.drop_column("gift_aid_declarations", "has_evidence_image")
