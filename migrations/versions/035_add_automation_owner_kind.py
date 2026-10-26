"""Add automations.owner_kind to tell service-account-owned automations apart.

Revision ID: 035
Revises: 034
Create Date: 2026-10-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "035"
down_revision: str = "034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # server_default backfills every existing row as human-owned.
    op.add_column(
        "automations",
        sa.Column(
            "owner_kind",
            sa.String(length=16),
            nullable=False,
            server_default="user",
        ),
    )


def downgrade() -> None:
    op.drop_column("automations", "owner_kind")
