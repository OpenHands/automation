"""Add task_outcome column to automation_runs table.

Revision ID: 015
Revises: 014
Create Date: 2026-08-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "015"
down_revision: str = "014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("automation_runs", sa.Column("task_outcome", sa.JSON, nullable=True))


def downgrade() -> None:
    op.drop_column("automation_runs", "task_outcome")
