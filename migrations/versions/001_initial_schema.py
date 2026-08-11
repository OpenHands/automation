"""Initial schema: automations and automation_runs tables.

All timestamp columns use TIMESTAMP WITH TIME ZONE (timestamptz) to enforce
UTC at the database level. PostgreSQL normalizes all values to UTC on write.

Cross-database compatible: works with both PostgreSQL and SQLite.

Revision ID: 001
Revises: None
Create Date: 2026-03-13
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    Uuid,
    text,
)


revision: str = "001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_sqlite() -> bool:
    """Check if we're running on SQLite."""
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    # Create automations table
    # Uses Uuid for cross-database UUID support
    # Uses JSON for cross-database JSON support (PostgreSQL uses JSONB internally)
    op.create_table(
        "automations",
        Column("id", Uuid, primary_key=True),
        Column("user_id", Uuid, nullable=False),
        Column("org_id", Uuid, nullable=False),
        Column("name", String(500), nullable=False),
        Column("trigger", JSON, nullable=False),
        Column("tarball_path", Text, nullable=False),
        Column("setup_script_path", Text, nullable=True),
        Column("entrypoint", Text, nullable=False),
        Column("timeout", Integer, nullable=True),
        Column("enabled", Boolean, nullable=False, server_default="true"),
        Column("deleted_at", DateTime(timezone=True), nullable=True),
        Column("last_triggered_at", DateTime(timezone=True), nullable=True),
        Column("last_polled_at", DateTime(timezone=True), nullable=True),
        Column(
            "created_at",
            DateTime(timezone=True),
            nullable=False,
            server_default=text("CURRENT_TIMESTAMP"),
        ),
        Column(
            "updated_at",
            DateTime(timezone=True),
            nullable=False,
            server_default=text("CURRENT_TIMESTAMP"),
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
        Column("id", Uuid, primary_key=True),
        Column(
            "automation_id",
            Uuid,
            ForeignKey("automations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        Column(
            "status",
            String(20),
            nullable=False,
            server_default="PENDING",
        ),
        Column("error_detail", Text, nullable=True),
        Column(
            "created_at",
            DateTime(timezone=True),
            nullable=False,
            server_default=text("CURRENT_TIMESTAMP"),
        ),
        Column("started_at", DateTime(timezone=True), nullable=True),
        Column("completed_at", DateTime(timezone=True), nullable=True),
        Column("conversation_id", String(255), nullable=True),
        Column("timeout_at", DateTime(timezone=True), nullable=True),
        Column("keep_alive", Boolean(), nullable=False, server_default="false"),
        Column("sandbox_id", String(255), nullable=True),
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
            postgresql_where=text("status = 'PENDING'"),
        )

    op.create_index("ix_automation_runs_timeout_at", "automation_runs", ["timeout_at"])


def downgrade() -> None:
    op.drop_table("automation_runs")
    op.drop_table("automations")
