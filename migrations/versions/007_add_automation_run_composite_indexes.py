"""Add composite indexes for automation run polling queries.

Revision ID: 007
Revises: 006
Create Date: 2026-06-05
"""

from collections.abc import Sequence

from alembic import op


revision: str = "007"
down_revision: str = "006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_automation_runs_status_created_at",
        "automation_runs",
        ["status", "created_at"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_automation_runs_status_timeout_at",
        "automation_runs",
        ["status", "timeout_at"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_automation_runs_status_timeout_at",
        table_name="automation_runs",
        if_exists=True,
    )
    op.drop_index(
        "ix_automation_runs_status_created_at",
        table_name="automation_runs",
        if_exists=True,
    )
