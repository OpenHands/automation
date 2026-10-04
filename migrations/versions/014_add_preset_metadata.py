"""Add preset_metadata column to automations table.

This migration adds a nullable preset_metadata JSON column to the automations
table so preset endpoints can record the configuration used to build the
automation (preset type, prompt, plugins, repos) for the UI to consume.
Custom SDK automations leave it NULL.

Revision ID: 014
Revises: 013
Create Date: 2026-08-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "014"
down_revision: str = "013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("automations", sa.Column("preset_metadata", sa.JSON, nullable=True))


def downgrade() -> None:
    op.drop_column("automations", "preset_metadata")
