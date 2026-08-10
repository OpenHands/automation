"""Persist automation health and structured run failure metadata.

Revision ID: 015
Revises: 014
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "015"
down_revision: str = "014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "automations",
        sa.Column(
            "consecutive_failure_count",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column("automations", sa.Column("disabled_reason", sa.Text, nullable=True))
    op.add_column(
        "automations",
        sa.Column("disabled_failure_kind", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "automation_runs",
        sa.Column("failure_kind", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "automation_runs", sa.Column("blocking_reason", sa.Text, nullable=True)
    )


def downgrade() -> None:
    op.drop_column("automation_runs", "blocking_reason")
    op.drop_column("automation_runs", "failure_kind")
    op.drop_column("automations", "disabled_failure_kind")
    op.drop_column("automations", "disabled_reason")
    op.drop_column("automations", "consecutive_failure_count")
