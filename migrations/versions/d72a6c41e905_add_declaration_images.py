"""Store optional base64 declaration image evidence."""

import sqlalchemy as sa
from alembic import op

revision = "d72a6c41e905"
down_revision = "c41f8a9d7302"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add encrypted image payload and allowlisted MIME metadata columns."""
    op.add_column("gift_aid_declarations", sa.Column("evidence_image_base64", sa.Text()))
    op.add_column("gift_aid_declarations", sa.Column("evidence_image_mime", sa.Text()))


def downgrade() -> None:
    """Remove declaration image evidence columns."""
    op.drop_column("gift_aid_declarations", "evidence_image_mime")
    op.drop_column("gift_aid_declarations", "evidence_image_base64")
