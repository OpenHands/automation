"""Add cleanup_at column for delayed sandbox cleanup.

Revision ID: 009
Revises: 008
Create Date: 2026-04-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "009"
down_revision: str = "008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "automation_runs",
        sa.Column("cleanup_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_automation_runs_cleanup_at", "automation_runs", ["cleanup_at"])


def downgrade() -> None:
    op.drop_index("ix_automation_runs_cleanup_at", table_name="automation_runs")
    op.drop_column("automation_runs", "cleanup_at")
