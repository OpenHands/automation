"""Add conversation_id, timeout_at, keep_alive, sandbox_id to automation_runs.

timeout_at: Pre-computed deadline for the staleness watchdog.
conversation_id: Set by the completion callback when SDK creates a conversation.
keep_alive: If True, sandbox is not deleted after run completes (for debugging).
sandbox_id: The sandbox ID used for execution (for status verification).

Revision ID: 003
Revises: 002
Create Date: 2026-03-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "003"
down_revision: str | None = "002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "automation_runs",
        sa.Column("conversation_id", sa.String(255), nullable=True),
    )
    op.add_column(
        "automation_runs",
        sa.Column(
            "timeout_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "automation_runs",
        sa.Column("keep_alive", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "automation_runs",
        sa.Column("sandbox_id", sa.String(255), nullable=True),
    )
    op.create_index(
        "ix_automation_runs_timeout_at",
        "automation_runs",
        ["timeout_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_automation_runs_timeout_at", table_name="automation_runs")
    op.drop_column("automation_runs", "sandbox_id")
    op.drop_column("automation_runs", "keep_alive")
    op.drop_column("automation_runs", "timeout_at")
    op.drop_column("automation_runs", "conversation_id")
