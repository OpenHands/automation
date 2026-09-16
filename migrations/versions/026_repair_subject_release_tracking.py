"""Repair databases that applied 022 before subject release tracking was added.

Revision ID: 026
Revises: 025
Create Date: 2026-09-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "026"
down_revision: str = "025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = sa.inspect(op.get_bind()).get_columns("automation_runs")
    if any(column["name"] == "subject_released_at" for column in columns):
        return  # The newer version of 022 already created the column and index.

    op.add_column(
        "automation_runs",
        sa.Column("subject_released_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.drop_index("ix_automation_runs_subject", table_name="automation_runs")
    where = sa.text("subject_key IS NOT NULL AND subject_released_at IS NULL")
    op.create_index(
        "ix_automation_runs_subject",
        "automation_runs",
        ["automation_id", "subject_key", "created_at"],
        unique=False,
        postgresql_where=where,
        sqlite_where=where,
    )


def downgrade() -> None:
    # Current 022 already owns this schema. Preserve it (and release history)
    # when returning to 022; that revision's downgrade removes both columns.
    pass
