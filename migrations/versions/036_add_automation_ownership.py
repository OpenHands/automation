"""Add service-account ownership and the change audit trail for automations.

- automations.owner_kind tells service-account-owned automations apart.
- automation_change_events records who changed an automation, and which fields.

Revision ID: 036
Revises: 035
Create Date: 2026-10-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "036"
down_revision: str = "035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # server_default backfills every existing row as human-owned. The ORM
    # stores enum member NAMES (native_enum=False), so the default is "USER",
    # not the API value "user".
    op.add_column(
        "automations",
        sa.Column(
            "owner_kind",
            sa.String(length=16),
            nullable=False,
            server_default="USER",
        ),
    )

    op.create_table(
        "automation_change_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        # No FK: an audit row must outlive its automation, also a hard delete.
        sa.Column("automation_id", sa.Uuid(), nullable=False),
        # Copied from the automation, so reads stay org-scoped without a join
        # and still work after the automation is gone.
        sa.Column("org_id", sa.Uuid(), nullable=False),
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
        sa.PrimaryKeyConstraint("id"),
    )
    # Per-automation timeline, newest first.
    op.create_index(
        "ix_automation_change_events_automation_created",
        "automation_change_events",
        ["automation_id", "created_at"],
    )
    # Org-wide view and retention purges.
    op.create_index(
        "ix_automation_change_events_org_created",
        "automation_change_events",
        ["org_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_automation_change_events_org_created",
        table_name="automation_change_events",
    )
    op.drop_index(
        "ix_automation_change_events_automation_created",
        table_name="automation_change_events",
    )
    op.drop_table("automation_change_events")
    op.drop_column("automations", "owner_kind")
