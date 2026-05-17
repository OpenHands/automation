"""Add llm_profile column to automations table.

Revision ID: 005
Revises: 004
Create Date: 2026-05-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "005"
down_revision: str = "004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("automations", sa.Column("llm_profile", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("automations", "llm_profile")
