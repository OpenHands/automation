"""Add the subject a run is about, and when it stopped owning it.

Revision ID: 022
Revises: 021
Create Date: 2026-08-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "022"
down_revision: str = "021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "automation_runs",
        sa.Column("subject_key", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "automation_runs",
        sa.Column("subject_released_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Partial: only live subjects are ever looked up, and only
    # `continue_conversation` runs set one.
    where = "subject_key IS NOT NULL AND subject_released_at IS NULL"
    op.create_index(
        "ix_automation_runs_subject",
        "automation_runs",
        ["automation_id", "subject_key", "created_at"],
        unique=False,
        postgresql_where=sa.text(where),
        sqlite_where=sa.text(where),
    )


def downgrade() -> None:
    op.drop_index("ix_automation_runs_subject", table_name="automation_runs")
    op.drop_column("automation_runs", "subject_released_at")
    op.drop_column("automation_runs", "subject_key")
