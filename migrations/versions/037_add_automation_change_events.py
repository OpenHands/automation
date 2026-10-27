"""Add automation_change_events, the audit trail of changes to an automation.

Revision ID: 037
Revises: 036
Create Date: 2026-10-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "037"
down_revision: str = "036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "automation_change_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("automation_id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        # The ORM stores enum member NAMES (native_enum=False).
        sa.Column("actor_kind", sa.String(length=16), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("changed_fields", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["automation_id"], ["automations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_automation_change_events_automation_id",
        "automation_change_events",
        ["automation_id"],
    )
    op.create_index(
        "ix_automation_change_events_created_at",
        "automation_change_events",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_automation_change_events_created_at",
        table_name="automation_change_events",
    )
    op.drop_index(
        "ix_automation_change_events_automation_id",
        table_name="automation_change_events",
    )
    op.drop_table("automation_change_events")
