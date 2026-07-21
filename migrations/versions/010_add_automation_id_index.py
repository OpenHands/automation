"""Add index on automation_runs.automation_id.

Revision ID: 010
Revises: 009
Create Date: 2026-07-21
"""

from collections.abc import Sequence

from alembic import op


revision: str = "010"
down_revision: str = "009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # automation_id is declared index=True on the AutomationRun model, but the
    # index is absent in production, so queries that filter runs by automation_id
    # (e.g. the list-runs count + fetch) sequentially scan the whole table.
    op.create_index(
        "ix_automation_runs_automation_id",
        "automation_runs",
        ["automation_id"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_automation_runs_automation_id",
        table_name="automation_runs",
        if_exists=True,
    )
