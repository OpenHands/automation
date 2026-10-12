"""add plugin spec runtime fields

Revision ID: 033_add_plugin_spec_runtime
Revises: 032_link_runs_to_triggering_events
Create Date: 2026-10-12 17:20:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "033_add_plugin_spec_runtime"
down_revision: str | None = "032_link_runs_to_triggering_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("automations", sa.Column("plugin_spec", sa.JSON(), nullable=True))
    op.add_column(
        "automation_runs", sa.Column("resolved_runtime", sa.JSON(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("automation_runs", "resolved_runtime")
    op.drop_column("automations", "plugin_spec")
