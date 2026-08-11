"""Add prompt column to automations table.

This migration adds a nullable prompt column to the automations table
so the prompt text is stored directly on the record and returned via API,
rather than only being embedded in the tarball.

Revision ID: 004
Revises: 003
Create Date: 2026-04-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "004"
down_revision: str = "003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("automations", sa.Column("prompt", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("automations", "prompt")
