"""Add support for event-triggered automations.

- Add event_payload column to automation_runs to store the event that triggered the run

Revision ID: 004
Revises: 003
Create Date: 2026-04-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "004"
down_revision: str = "003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Add event_payload column to automation_runs
    # This stores the event data that triggered an event-based automation
    op.add_column(
        "automation_runs",
        sa.Column("event_payload", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("automation_runs", "event_payload")
