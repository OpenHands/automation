"""Initial schema: automations and automation_runs tables.

All timestamp columns use TIMESTAMP WITH TIME ZONE (timestamptz) to enforce
UTC at the database level. PostgreSQL normalizes all values to UTC on write.

Cross-database compatible: works with both PostgreSQL and SQLite.

Revision ID: 001
Revises: None
Create Date: 2026-03-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_sqlite() -> bool:
    """Check if we're running on SQLite."""
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    # Create automations table
    # Uses sa.Uuid for cross-database UUID support
    # Uses sa.JSON for cross-database JSON support (PostgreSQL uses JSONB internally)
    op.create_table(
        "automations",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("user_id", sa.Uuid, nullable=False),
        sa.Column("org_id", sa.Uuid, nullable=False),
        sa.Column("name", sa.String(500), nullable=False),
        sa.Column("trigger", sa.JSON, nullable=False),
        sa.Column("tarball_path", sa.Text, nullable=False),
        sa.Column("setup_script_path", sa.Text, nullable=True),
        sa.Column("entrypoint", sa.Text, nullable=False),
        sa.Column("timeout", sa.Integer, nullable=True),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_triggered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index("ix_automations_user_id", "automations", ["user_id"])
    op.create_index("ix_automations_org_id", "automations", ["org_id"])
    op.create_index("ix_automations_enabled", "automations", ["enabled"])
    op.create_index("ix_automations_deleted_at", "automations", ["deleted_at"])
    op.create_index("ix_automations_last_polled_at", "automations", ["last_polled_at"])

    # Create automation_runs table (event queue + history)
    op.create_table(
        "automation_runs",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column(
            "automation_id",
            sa.Uuid,
            sa.ForeignKey("automations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("error_detail", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("conversation_id", sa.String(255), nullable=True),
        sa.Column("timeout_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("keep_alive", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("sandbox_id", sa.String(255), nullable=True),
    )
    op.create_index(
        "ix_automation_runs_automation_id", "automation_runs", ["automation_id"]
    )
    op.create_index("ix_automation_runs_status", "automation_runs", ["status"])

    # Partial index for efficient PENDING polling (PostgreSQL only)
    # SQLite doesn't support partial indexes in the same way, so skip for SQLite
    if not _is_sqlite():
        op.create_index(
            "ix_automation_runs_pending",
            "automation_runs",
            ["created_at"],
            postgresql_where=sa.text("status = 'PENDING'"),
        )

    op.create_index("ix_automation_runs_timeout_at", "automation_runs", ["timeout_at"])


def downgrade() -> None:
    op.drop_table("automation_runs")
    op.drop_table("automations")
