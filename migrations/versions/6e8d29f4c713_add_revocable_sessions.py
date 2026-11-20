"""Add independently revocable browser sessions with bounded lifetimes."""

import sqlalchemy as sa
from alembic import op


revision = "6e8d29f4c713"
down_revision = "2026b0719a01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the server-side session registry and its expiry lookup index.

    The registry permits individual browser sessions to be revoked without changing
    the account password or invalidating unrelated authenticated devices.
    """
    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("last_seen_at", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])


def downgrade() -> None:
    """Remove the revocable-session schema when rolling back this migration.

    The index is dropped before its owning table so the downgrade remains explicit
    and portable across Alembic-supported SQLite configurations.
    """
    op.drop_index("ix_auth_sessions_user_id", table_name="auth_sessions")
    op.drop_table("auth_sessions")
