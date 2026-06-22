"""Add bash_command_id column to automation_runs table.

Records the agent-server BashCommand id assigned when an automation's bash
chain is dispatched. The verifier (watchdog) uses it to filter BashOutput
events by ``command_id__eq=<hex>`` so it picks up *this run's* output rather
than whatever BashOutput happened to land on the agent-server most recently
— a real cross-contamination hazard in local mode where a single agent-
server is shared across runs (and the agent's own TerminalTool).

Revision ID: 005
Revises: 004
Create Date: 2026-05-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "005"
down_revision: str = "004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 32-char UUID hex is the only thing we ever store here; 64 leaves room
    # if the agent-server's command id representation ever widens.
    op.add_column(
        "automation_runs",
        sa.Column("bash_command_id", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("automation_runs", "bash_command_id")
