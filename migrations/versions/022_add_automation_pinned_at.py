"""Add pinned_at column to automations.

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


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    op.add_column(
        "automations",
        sa.Column("pinned_at", sa.DateTime(timezone=True), nullable=True),
    )

    if _is_sqlite():
        return

    op.execute(
        "COMMENT ON COLUMN automations.pinned_at IS "
        "'Timestamp when the automation was pinned. NULL means unpinned. "
        "Ordered by recency to determine pin order.'"
    )


def downgrade() -> None:
    op.drop_column("automations", "pinned_at")
