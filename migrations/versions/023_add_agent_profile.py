"""Persist automation profile selection and snapshot it on queued runs.

Revision ID: 023
Revises: 022
"""

import sqlalchemy as sa
from alembic import op


revision = "023"
down_revision = "022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("automations", "automation_runs"):
        op.add_column(table, sa.Column("agent_profile_id", sa.Uuid(), nullable=True))


def downgrade() -> None:
    for table in ("automation_runs", "automations"):
        op.drop_column(table, "agent_profile_id")
