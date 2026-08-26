"""Add live run progress phase.

Revision ID: 020
Revises: 019
Create Date: 2026-08-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "020"
down_revision: str = "019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    op.add_column(
        "automation_runs",
        sa.Column("current_phase", sa.String(length=200), nullable=True),
    )

    if _is_sqlite():
        return

    op.execute(
        "COMMENT ON COLUMN automation_runs.current_phase IS "
        "'Human-readable live progress phase reported while the run is "
        "PENDING/RUNNING. Not cleared on completion; consumers must only "
        "render it for in-flight runs.'"
    )


def downgrade() -> None:
    op.drop_column("automation_runs", "current_phase")
