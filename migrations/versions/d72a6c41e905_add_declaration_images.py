"""Store optional base64 declaration image evidence."""

import sqlalchemy as sa
from alembic import op

revision = "d72a6c41e905"
down_revision = "c41f8a9d7302"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add declaration-image payload and MIME metadata columns.

    Alembic calls ``upgrade`` once when moving an existing database to this
    revision.  The physical payload column is TEXT; encryption is applied by the
    SQLAlchemy ``EncryptedText`` type when application code reads or writes it.
    Both columns are nullable so existing declarations remain valid.
    """
    op.add_column("gift_aid_declarations", sa.Column("evidence_image_base64", sa.Text()))
    op.add_column("gift_aid_declarations", sa.Column("evidence_image_mime", sa.Text()))


def downgrade() -> None:
    """Reverse this revision, permanently removing stored image evidence."""
    op.drop_column("gift_aid_declarations", "evidence_image_mime")
    op.drop_column("gift_aid_declarations", "evidence_image_base64")
