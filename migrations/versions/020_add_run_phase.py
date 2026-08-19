"""Add phase columns to automation_runs table.

Revision ID: 020
Revises: 019
Create Date: 2026-08-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "020"
down_revision: str = "019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "automation_runs", sa.Column("phase_code", sa.String(128), nullable=True)
    )
    op.add_column(
        "automation_runs", sa.Column("phase_label", sa.String(200), nullable=True)
    )
    op.add_column(
        "automation_runs",
        sa.Column("phase_updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("automation_runs", "phase_updated_at")
    op.drop_column("automation_runs", "phase_label")
    op.drop_column("automation_runs", "phase_code")
