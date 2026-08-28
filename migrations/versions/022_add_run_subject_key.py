"""Add automation_runs.subject_key: the external subject a run is about.

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
    # Partial: only `continue_conversation` runs set a subject.
    op.create_index(
        "ix_automation_runs_subject",
        "automation_runs",
        ["automation_id", "subject_key", "created_at"],
        unique=False,
        postgresql_where=sa.text("subject_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_automation_runs_subject", table_name="automation_runs")
    op.drop_column("automation_runs", "subject_key")
