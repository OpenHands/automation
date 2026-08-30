"""Add automation disabled reason metadata.

Revision ID: 020
Revises: 019
Create Date: 2026-08-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "020"
down_revision: str = "019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    op.add_column("automations", sa.Column("disabled_reason", sa.Text(), nullable=True))
    op.add_column("automations", sa.Column("disabled_detail", sa.JSON(), nullable=True))
    op.add_column(
        "automations",
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "automation_disable_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("automation_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["automation_id"], ["automations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["automation_runs.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_automation_disable_events_automation_id",
        "automation_disable_events",
        ["automation_id"],
    )
    op.create_index(
        "ix_automation_disable_events_run_id",
        "automation_disable_events",
        ["run_id"],
    )
    op.create_index(
        "ix_automation_disable_events_created_at",
        "automation_disable_events",
        ["created_at"],
    )

    if _is_sqlite():
        return

    op.execute(
        "COMMENT ON COLUMN automations.disabled_reason IS "
        "'Human-readable reason an automation is currently disabled.'"
    )
    op.execute(
        "COMMENT ON COLUMN automations.disabled_detail IS "
        "'Structured metadata for current automation disabled state.'"
    )
    op.execute(
        "COMMENT ON TABLE automation_disable_events IS "
        "'Historical records for automation auto-disable decisions.'"
    )


def downgrade() -> None:
    op.drop_index(
        "ix_automation_disable_events_created_at",
        table_name="automation_disable_events",
    )
    op.drop_index(
        "ix_automation_disable_events_run_id",
        table_name="automation_disable_events",
    )
    op.drop_index(
        "ix_automation_disable_events_automation_id",
        table_name="automation_disable_events",
    )
    op.drop_table("automation_disable_events")
    op.drop_column("automations", "disabled_at")
    op.drop_column("automations", "disabled_detail")
    op.drop_column("automations", "disabled_reason")
