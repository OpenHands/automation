"""Snapshot the source executed by each automation run.

Revision ID: 023
Revises: 022
Create Date: 2026-09-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "023"
down_revision: str = "022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    op.add_column(
        "automation_runs",
        sa.Column("source_tarball_path", sa.Text(), nullable=True),
    )
    op.add_column(
        "automation_runs",
        sa.Column("source_commit", sa.String(length=64), nullable=True),
    )

    if _is_sqlite():
        return

    op.execute(
        "COMMENT ON COLUMN automation_runs.source_tarball_path IS "
        "'Tarball path frozen when this run is dispatched; identifies the "
        "source selected for execution.'"
    )
    op.execute(
        "COMMENT ON COLUMN automation_runs.source_commit IS "
        "'Git commit frozen when this run is dispatched, when the automation "
        "is fully reconciled with git.'"
    )


def downgrade() -> None:
    op.drop_column("automation_runs", "source_commit")
    op.drop_column("automation_runs", "source_tarball_path")
