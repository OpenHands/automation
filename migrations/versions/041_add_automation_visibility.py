"""Let an automation be private to the person it runs as, and to managers.

Revision ID: 041
Revises: 040
Create Date: 2026-11-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "041"
down_revision: str = "040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # server_default keeps every existing row visible to its organization. The
    # ORM stores enum member NAMES (native_enum=False), so the default is
    # "ORGANIZATION", not the API value "organization".
    op.add_column(
        "automations",
        sa.Column(
            "visibility",
            sa.String(length=16),
            nullable=False,
            server_default="ORGANIZATION",
        ),
    )


def downgrade() -> None:
    op.drop_column("automations", "visibility")
