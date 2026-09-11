"""Add automation state and run trigger_source.

Revision ID: 023
Revises: 022
Create Date: 2026-09-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "023"
down_revision: str = "022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "automations",
        sa.Column(
            "state",
            sa.String(length=20),
            nullable=False,
            server_default="ACTIVE",
        ),
    )
    op.create_index(
        "ix_automations_state", "automations", ["state"]
    )
    # Backfill from the legacy enabled flag so existing rows match the new
    # state model on day one.
    op.execute(
        "UPDATE automations SET state = CASE "
        "WHEN enabled THEN 'ACTIVE' ELSE 'INACTIVE' END"
    )

    op.add_column(
        "automation_runs",
        sa.Column("trigger_source", sa.String(length=32), nullable=True),
    )
    op.create_index(
        "ix_automation_runs_trigger_source", "automation_runs", ["trigger_source"]
    )
    op.create_index(
        "ix_automation_runs_status_trigger_source",
        "automation_runs",
        ["status", "trigger_source"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_automation_runs_status_trigger_source", table_name="automation_runs"
    )
    op.drop_index("ix_automation_runs_trigger_source", table_name="automation_runs")
    op.drop_column("automation_runs", "trigger_source")

    op.drop_index("ix_automations_state", table_name="automations")
    op.drop_column("automations", "state")
