"""Initial schema: automations and automation_runs tables.

All timestamp columns use TIMESTAMP WITH TIME ZONE (timestamptz) to enforce
UTC at the database level. PostgreSQL normalizes all values to UTC on write.

Revision ID: 001
Revises: None
Create Date: 2026-03-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision: str = "001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Create automations table
    op.create_table(
        "automations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("org_id", UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(500), nullable=False),
        sa.Column("trigger", JSONB, nullable=False),
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
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "automation_id",
            UUID(as_uuid=True),
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
    )
    op.create_index(
        "ix_automation_runs_automation_id", "automation_runs", ["automation_id"]
    )
    op.create_index("ix_automation_runs_status", "automation_runs", ["status"])
    # Partial index for efficient PENDING polling (PostgreSQL only)
    op.create_index(
        "ix_automation_runs_pending",
        "automation_runs",
        ["created_at"],
        postgresql_where=sa.text("status = 'PENDING'"),
    )


def downgrade() -> None:
    op.drop_table("automation_runs")
    op.drop_table("automations")
