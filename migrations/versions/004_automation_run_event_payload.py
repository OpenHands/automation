"""Add event_payload column to automation_runs table.

Stores the webhook payload that triggered event-based automation runs.
For GitHub events: model_dump() of parsed Pydantic event
For custom webhooks: the raw payload dict

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
    op.add_column(
        "automation_runs",
        sa.Column("event_payload", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("automation_runs", "event_payload")
