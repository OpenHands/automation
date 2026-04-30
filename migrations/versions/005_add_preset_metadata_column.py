"""Add preset_metadata column to automations table.

This migration adds a nullable JSON preset_metadata column to the automations
table for storing preset-specific configuration that the UI can consume.

The field stores metadata like preset_type, prompt, plugins, and repos
for automations created via preset endpoints (/v1/preset/prompt, /v1/preset/plugin).
Custom SDK automations will have NULL preset_metadata.

Revision ID: 005
Revises: 004
Create Date: 2026-04-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "005"
down_revision: str = "004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "automations",
        sa.Column("preset_metadata", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("automations", "preset_metadata")
