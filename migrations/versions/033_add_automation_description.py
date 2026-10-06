"""Add an optional human-readable automation description.

Revision ID: 033
Revises: 032
Create Date: 2026-09-28
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import Column, Text


revision: str = "033"
down_revision: str = "032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("automations", Column("description", Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("automations", "description")
