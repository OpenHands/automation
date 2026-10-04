"""Add service metadata table.

Revision ID: 011
Revises: 010
Create Date: 2026-07-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "011"
down_revision: str = "010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    op.create_table(
        "automation_service_metadata",
        sa.Column("key", sa.String(255), primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )

    if _is_sqlite():
        return

    op.execute(
        "COMMENT ON TABLE automation_service_metadata IS "
        "'Service-level metadata shared across automation deployment modes. "
        "Stores singleton values such as the PostHog backend distinct ID.'"
    )


def downgrade() -> None:
    op.drop_table("automation_service_metadata")
