"""Add cost column to automation_runs table.

Revision ID: 013
Revises: 012
Create Date: 2026-07-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "013"
down_revision: str = "012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("automation_runs", sa.Column("cost", sa.Float, nullable=True))


def downgrade() -> None:
    op.drop_column("automation_runs", "cost")
