"""Add run deferral state for limit-blocked runs.

- automation_runs.deferred_until is stamped when a run blocked by a transient
  limit (organization concurrency cap, daily sandbox quota) is returned to
  PENDING instead of being marked terminal SKIPPED. The dispatch poll skips a
  PENDING run whose deferred_until is still in the future.
- automation_runs.deferral_count counts those deferrals; it drives the bounded
  exponential backoff between retries.

Revision ID: 038
Revises: 037
Create Date: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "038"
down_revision: str = "037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "automation_runs",
        sa.Column("deferred_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "automation_runs",
        sa.Column(
            "deferral_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    # Partial: NULL on nearly every row; only stamped while a run is deferred
    # by a limit, and cleared once the run is claimed.
    where = "deferred_until IS NOT NULL"
    op.create_index(
        "ix_automation_runs_deferred_until",
        "automation_runs",
        ["deferred_until"],
        unique=False,
        postgresql_where=sa.text(where),
        sqlite_where=sa.text(where),
    )


def downgrade() -> None:
    op.drop_index("ix_automation_runs_deferred_until", table_name="automation_runs")
    op.drop_column("automation_runs", "deferral_count")
    op.drop_column("automation_runs", "deferred_until")
