"""Add key-value store for automation state persistence.

This migration adds:
1. enable_kv_store column to automations table (opt-in flag)
2. automation_kv table for storing encrypted key-value pairs

Storage Design Decision:
    We use BYTEA (LargeBinary) for encrypted values instead of TEXT because:
    - Encrypted data is binary, not text (AES-GCM produces raw bytes)
    - BYTEA avoids the ~33% overhead of base64 encoding
    - Better alignment with PostgreSQL's TOAST compression for binary data
    - See automation/utils/kv.py for full encryption design rationale

Revision ID: 005
Revises: 004
Create Date: 2026-04-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "005"
down_revision: str = "004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Add enable_kv_store column to automations table
    op.add_column(
        "automations",
        sa.Column(
            "enable_kv_store", sa.Boolean, nullable=False, server_default="false"
        ),
    )

    # Create automation_kv table
    # Note: value_encrypted is BYTEA (LargeBinary) for efficient binary storage.
    # See module docstring for design rationale.
    op.create_table(
        "automation_kv",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column(
            "automation_id",
            sa.Uuid,
            sa.ForeignKey("automations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("value_encrypted", sa.LargeBinary, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )

    # Create unique index on (automation_id, key)
    op.create_index(
        "ix_automation_kv_automation_key",
        "automation_kv",
        ["automation_id", "key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_automation_kv_automation_key", table_name="automation_kv")
    op.drop_table("automation_kv")
    op.drop_column("automations", "enable_kv_store")
