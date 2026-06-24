"""Move sandbox cleanup configuration to automations.

Revision ID: 009
Revises: 008
Create Date: 2026-06-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "009"
down_revision: str = "008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_sqlite() -> bool:
    """Check if we are running against SQLite (test/dev only)."""
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    op.drop_column("automation_runs", "keep_alive")
    op.add_column(
        "automations",
        sa.Column("keep_alive", sa.Boolean(), nullable=True),
    )

    if not _is_sqlite():
        op.execute(
            "COMMENT ON COLUMN automations.keep_alive IS "
            "'If true, the automation service leaves run sandboxes for runtime "
            "TTL cleanup. If false or null, the service explicitly deletes "
            "sandboxes after completion, or after post-run callbacks when "
            "configured.'"
        )


def downgrade() -> None:
    op.drop_column("automations", "keep_alive")
    op.add_column(
        "automation_runs",
        sa.Column("keep_alive", sa.Boolean(), nullable=False, server_default="false"),
    )
