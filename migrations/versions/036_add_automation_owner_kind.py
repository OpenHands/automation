"""Add automations.owner_kind to tell service-account-owned automations apart.

Revision ID: 036
Revises: 035
Create Date: 2026-10-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "036"
down_revision: str = "035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # server_default backfills every existing row as human-owned. The ORM
    # stores enum member NAMES (native_enum=False), so the default is "USER",
    # not the API value "user".
    op.add_column(
        "automations",
        sa.Column(
            "owner_kind",
            sa.String(length=16),
            nullable=False,
            server_default="USER",
        ),
    )


def downgrade() -> None:
    op.drop_column("automations", "owner_kind")
