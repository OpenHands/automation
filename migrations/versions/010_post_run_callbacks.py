"""Add post-run callback configuration and execution records.

Revision ID: 010
Revises: 009
Create Date: 2026-06-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "010"
down_revision: str = "009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    op.add_column("automations", sa.Column("callbacks", sa.JSON(), nullable=True))

    op.create_table(
        "automation_run_callbacks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("trigger_status", sa.String(length=20), nullable=False),
        sa.Column("entrypoint", sa.Text(), nullable=False),
        sa.Column("timeout", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("bash_command_id", sa.String(length=64), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("order", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("timeout_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["run_id"], ["automation_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_automation_run_callbacks_run_id",
        "automation_run_callbacks",
        ["run_id"],
    )
    op.create_index(
        "ix_automation_run_callbacks_status",
        "automation_run_callbacks",
        ["status"],
    )
    op.create_index(
        "ix_automation_run_callbacks_timeout_at",
        "automation_run_callbacks",
        ["timeout_at"],
    )
    op.create_index(
        "ix_automation_run_callbacks_run_order",
        "automation_run_callbacks",
        ["run_id", "order"],
    )
    op.create_index(
        "ix_automation_run_callbacks_status_timeout",
        "automation_run_callbacks",
        ["status", "timeout_at"],
    )

    if not _is_sqlite():
        op.execute(
            "COMMENT ON COLUMN automations.callbacks IS "
            "'Post-run callback configuration for automation runs.'"
        )


def downgrade() -> None:
    op.drop_index(
        "ix_automation_run_callbacks_status_timeout",
        table_name="automation_run_callbacks",
    )
    op.drop_index(
        "ix_automation_run_callbacks_run_order",
        table_name="automation_run_callbacks",
    )
    op.drop_index(
        "ix_automation_run_callbacks_timeout_at",
        table_name="automation_run_callbacks",
    )
    op.drop_index(
        "ix_automation_run_callbacks_status",
        table_name="automation_run_callbacks",
    )
    op.drop_index(
        "ix_automation_run_callbacks_run_id",
        table_name="automation_run_callbacks",
    )
    op.drop_table("automation_run_callbacks")
    op.drop_column("automations", "callbacks")
