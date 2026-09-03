"""Add automation drafts and run trigger source.

Revision ID: 022
Revises: 021
Create Date: 2026-09-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "022"
down_revision: str = "021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    op.add_column(
        "automations",
        sa.Column(
            "lifecycle_status",
            sa.String(length=20),
            nullable=False,
            server_default="ACTIVE",
        ),
    )
    op.create_index(
        "ix_automations_lifecycle_status", "automations", ["lifecycle_status"]
    )
    op.execute(
        "UPDATE automations SET lifecycle_status = CASE "
        "WHEN enabled THEN 'ACTIVE' ELSE 'INACTIVE' END"
    )

    op.add_column(
        "automation_runs",
        sa.Column("trigger_source", sa.String(length=32), nullable=True),
    )
    op.create_index(
        "ix_automation_runs_trigger_source", "automation_runs", ["trigger_source"]
    )
    op.create_index(
        "ix_automation_runs_status_trigger_source",
        "automation_runs",
        ["status", "trigger_source"],
    )

    op.create_table(
        "automation_drafts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("endpoint", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=500), nullable=True),
        sa.Column("draft_body", sa.JSON(), nullable=False),
        sa.Column("validation_errors", sa.JSON(), nullable=True),
        sa.Column("dispatchable", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("source_automation_id", sa.Uuid(), nullable=True),
        sa.Column("materialized_automation_id", sa.Uuid(), nullable=True),
        sa.Column("last_test_run_id", sa.Uuid(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["source_automation_id"], ["automations.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["materialized_automation_id"], ["automations.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["last_test_run_id"], ["automation_runs.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_automation_drafts_user_id", "automation_drafts", ["user_id"])
    op.create_index("ix_automation_drafts_org_id", "automation_drafts", ["org_id"])
    op.create_index(
        "ix_automation_drafts_deleted_at", "automation_drafts", ["deleted_at"]
    )
    op.create_index(
        "ix_automation_drafts_org_updated_at",
        "automation_drafts",
        ["org_id", "updated_at"],
    )
    op.create_index(
        "ix_automation_drafts_org_deleted_at",
        "automation_drafts",
        ["org_id", "deleted_at"],
    )
    op.create_index(
        "ix_automation_drafts_source_automation_id",
        "automation_drafts",
        ["source_automation_id"],
    )
    op.create_index(
        "ix_automation_drafts_materialized_automation_id",
        "automation_drafts",
        ["materialized_automation_id"],
    )
    op.create_index(
        "ix_automation_drafts_last_test_run_id",
        "automation_drafts",
        ["last_test_run_id"],
    )

    if _is_sqlite():
        return

    op.execute(
        "COMMENT ON COLUMN automations.lifecycle_status IS "
        "'Automation state: ACTIVE, INACTIVE, or DRAFT.'"
    )
    op.execute(
        "COMMENT ON COLUMN automation_runs.trigger_source IS "
        "'How the run was created: manual, cron, event, or NULL for legacy rows.'"
    )
    op.execute(
        "COMMENT ON TABLE automation_drafts IS "
        "'Incomplete or complete automation setup drafts saved by setup UIs.'"
    )


def downgrade() -> None:
    op.drop_index(
        "ix_automation_drafts_last_test_run_id", table_name="automation_drafts"
    )
    op.drop_index(
        "ix_automation_drafts_materialized_automation_id",
        table_name="automation_drafts",
    )
    op.drop_index(
        "ix_automation_drafts_source_automation_id", table_name="automation_drafts"
    )
    op.drop_index("ix_automation_drafts_org_deleted_at", table_name="automation_drafts")
    op.drop_index("ix_automation_drafts_org_updated_at", table_name="automation_drafts")
    op.drop_index("ix_automation_drafts_deleted_at", table_name="automation_drafts")
    op.drop_index("ix_automation_drafts_org_id", table_name="automation_drafts")
    op.drop_index("ix_automation_drafts_user_id", table_name="automation_drafts")
    op.drop_table("automation_drafts")

    op.drop_index(
        "ix_automation_runs_status_trigger_source", table_name="automation_runs"
    )
    op.drop_index("ix_automation_runs_trigger_source", table_name="automation_runs")
    op.drop_column("automation_runs", "trigger_source")

    op.drop_index("ix_automations_lifecycle_status", table_name="automations")
    op.drop_column("automations", "lifecycle_status")
