"""Persist git sync transient failure log throttling across replicas.

Revision ID: 041
Revises: 040
Create Date: 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "041"
down_revision: str = "040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "automation_git_sync_org_config"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("transient_failure_fingerprint", sa.String(64)))
    op.add_column(
        _TABLE,
        sa.Column(
            "consecutive_transient_failures",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column("consecutive_transient_failures")
        batch.drop_column("transient_failure_fingerprint")
