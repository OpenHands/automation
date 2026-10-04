"""Add telemetry attribution to automations and runs.

Revision ID: 012
Revises: 011
Create Date: 2026-07-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "012"
down_revision: str = "011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "automations",
        sa.Column("telemetry_distinct_id", sa.String(length=256), nullable=True),
    )
    op.add_column(
        "automation_runs",
        sa.Column("telemetry_distinct_id", sa.String(length=256), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("automation_runs", "telemetry_distinct_id")
    op.drop_column("automations", "telemetry_distinct_id")
