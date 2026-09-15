"""Add when a run's sandbox is due for deferred deletion.

Revision ID: 024
Revises: 023
Create Date: 2026-09-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "024"
down_revision: str = "023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "automation_runs",
        sa.Column("sandbox_cleanup_due_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Partial: NULL on nearly every row; only stamped when the service defers
    # sandbox deletion, and cleared once the sandbox is gone.
    where = "sandbox_cleanup_due_at IS NOT NULL"
    op.create_index(
        "ix_automation_runs_sandbox_cleanup_due",
        "automation_runs",
        ["sandbox_cleanup_due_at"],
        unique=False,
        postgresql_where=sa.text(where),
        sqlite_where=sa.text(where),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_automation_runs_sandbox_cleanup_due", table_name="automation_runs"
    )
    op.drop_column("automation_runs", "sandbox_cleanup_due_at")
