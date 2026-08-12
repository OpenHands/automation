"""Add run_metadata column to automation_runs.

Stores additional execution metadata captured after a run completes, such as
structured semantic task outcomes parsed from preset conversation finish actions.

Revision ID: 015
Revises: 014
Create Date: 2026-08-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "015"
down_revision: str = "014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("automation_runs", sa.Column("run_metadata", sa.JSON, nullable=True))


def downgrade() -> None:
    op.drop_column("automation_runs", "run_metadata")
