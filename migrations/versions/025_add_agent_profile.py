"""Persist automation profile selection.

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
    op.add_column(
        "automations", sa.Column("agent_profile_id", sa.Uuid(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("automations", "agent_profile_id")
