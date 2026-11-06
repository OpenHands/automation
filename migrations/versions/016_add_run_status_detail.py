"""Add structured automation run status detail.

Revision ID: 016
Revises: 015
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "016"
down_revision: str = "015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    op.add_column(
        "automation_runs",
        sa.Column("status_detail", sa.JSON(), nullable=True),
    )

    if _is_sqlite():
        return

    op.execute(
        "COMMENT ON COLUMN automation_runs.status_detail IS "
        "'Structured current/last run lifecycle detail. Can describe "
        "transient infrastructure issues or terminal failure context.'"
    )


def downgrade() -> None:
    op.drop_column("automation_runs", "status_detail")
