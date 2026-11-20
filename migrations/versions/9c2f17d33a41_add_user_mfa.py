"""add user MFA and mandatory password-change state

Revision ID: 9c2f17d33a41
Revises: 5474a04de6d8
Create Date: 2026-11-16 12:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

import club_gift_aid_manager.models.types


revision: str = "9c2f17d33a41"
down_revision: Union[str, Sequence[str], None] = "5474a04de6d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create isolated encrypted MFA enrollment and recovery state."""
    op.add_column(
        "users",
        sa.Column(
            "must_change_password", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
    )
    op.create_table(
        "user_mfa",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("secret", club_gift_aid_manager.models.types.EncryptedText(), nullable=False),
        sa.Column("recovery_code_hashes", sa.Text(), nullable=False),
        sa.Column("last_totp_step", sa.Integer(), nullable=True),
        sa.Column("failed_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("locked_until", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("enabled_at", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.Text(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )


def downgrade() -> None:
    """Remove MFA enrollment state without altering user accounts."""
    op.drop_table("user_mfa")
    op.drop_column("users", "must_change_password")
