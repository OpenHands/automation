"""add automation tags

Revision ID: 034
Revises: 033
Create Date: 2026-10-21 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "034"
down_revision: str | None = "033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # NOT NULL with an empty-array default: PostgreSQL's json_array_elements_text
    # raises on a JSON null, and the tag facet query expands every row.
    op.add_column(
        "automations",
        sa.Column("tags", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )


def downgrade() -> None:
    op.drop_column("automations", "tags")
