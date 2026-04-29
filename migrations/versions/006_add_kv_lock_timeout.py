"""Add kv_lock_timeout_ms column to automations table.

Allows per-automation configuration of KV store lock timeout.
Default 5000ms (5 seconds) matches the hardcoded value from PR #69.

Revision ID: 006
Revises: 005_add_kv_store
Create Date: 2025-04-25
"""

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "006"
down_revision = "005_add_kv_store"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "automations",
        sa.Column(
            "kv_lock_timeout_ms",
            sa.Integer(),
            nullable=False,
            server_default="5000",
            comment="Lock timeout in ms for KV operations (100-30000, default 5000)",
        ),
    )


def downgrade() -> None:
    op.drop_column("automations", "kv_lock_timeout_ms")
