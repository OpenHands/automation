"""Drop callback_token from automation_runs.

The completion callback now authenticates via the same OPENHANDS_API_KEY
that was passed into the sandbox (validated by authenticate_request),
so the one-time callback_token is no longer needed.

Revision ID: 004
Revises: 003
Create Date: 2026-03-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "004"
down_revision: str | None = "003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("automation_runs", "callback_token")


def downgrade() -> None:
    op.add_column(
        "automation_runs",
        sa.Column("callback_token", sa.String(64), nullable=True),
    )
