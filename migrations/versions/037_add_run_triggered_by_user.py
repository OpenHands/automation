"""Record who started a manual automation run.

- automation_runs.triggered_by_user_id is the person who dispatched a manual
  run. Cron and event runs, and existing rows, stay null.

Revision ID: 037
Revises: 036
Create Date: 2026-10-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "037"
down_revision: str = "036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "automation_runs",
        sa.Column("triggered_by_user_id", sa.Uuid(), nullable=True),
    )


def downgrade() -> None:
    with op.batch_alter_table("automation_runs") as batch:
        batch.drop_column("triggered_by_user_id")
