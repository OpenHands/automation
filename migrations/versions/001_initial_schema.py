"""Initial schema: automations and automation_runs tables.

Revision ID: 001
Revises: None
Create Date: 2026-03-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSON, UUID


revision: str = "001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "automations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("name", sa.String(500), nullable=False),
        sa.Column("triggers", JSON, nullable=False),
        sa.Column("sdk_code_tarball_path", sa.Text, nullable=False),
        sa.Column("encrypted_api_key", sa.Text, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("last_triggered_at", sa.DateTime, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index("ix_automations_user_id", "automations", ["user_id"])
    op.create_index("ix_automations_enabled", "automations", ["enabled"])

    op.create_table(
        "automation_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("automation_id", UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("conversation_id", sa.String(255), nullable=True),
        sa.Column("trigger_type", sa.String(50), nullable=False, server_default="cron"),
        sa.Column("error_detail", sa.Text, nullable=True),
        sa.Column("started_at", sa.DateTime, nullable=True),
        sa.Column("completed_at", sa.DateTime, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "ix_automation_runs_automation_id", "automation_runs", ["automation_id"]
    )
    op.create_index("ix_automation_runs_status", "automation_runs", ["status"])
    op.create_index(
        "ix_automation_runs_automation_created",
        "automation_runs",
        ["automation_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("automation_runs")
    op.drop_table("automations")
