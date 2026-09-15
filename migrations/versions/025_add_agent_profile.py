"""Persist automation execution settings on definitions and queued runs.

Revision ID: 025
Revises: 024
"""

import sqlalchemy as sa
from alembic import op


revision = "025"
down_revision = "024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("automations", "automation_runs"):
        op.add_column(table, sa.Column("agent_profile_id", sa.Uuid(), nullable=True))
        op.add_column(
            table,
            sa.Column(
                "execution_scope",
                sa.String(20),
                nullable=False,
                server_default="run",
            ),
        )


def downgrade() -> None:
    for table in ("automation_runs", "automations"):
        op.drop_column(table, "execution_scope")
        op.drop_column(table, "agent_profile_id")
