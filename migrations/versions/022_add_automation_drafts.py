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

    with op.batch_alter_table("automations") as batch_op:
        batch_op.alter_column(
            "name", existing_type=sa.String(length=500), nullable=True
        )
        batch_op.alter_column("trigger", existing_type=sa.JSON(), nullable=True)
        batch_op.alter_column("tarball_path", existing_type=sa.Text(), nullable=True)
        batch_op.alter_column("entrypoint", existing_type=sa.Text(), nullable=True)
        batch_op.add_column(
            sa.Column("draft_endpoint", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(sa.Column("draft_body", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("validation_errors", sa.JSON(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "dispatchable",
                sa.Boolean(),
                nullable=False,
                server_default="false",
            )
        )
        batch_op.add_column(sa.Column("source_automation_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("last_test_run_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            "fk_automations_source_automation_id",
            "automations",
            ["source_automation_id"],
            ["id"],
            ondelete="SET NULL",
        )

    op.create_index(
        "ix_automations_org_lifecycle_updated_at",
        "automations",
        ["org_id", "lifecycle_status", "updated_at"],
    )
    op.create_index(
        "ix_automations_source_automation_id", "automations", ["source_automation_id"]
    )
    op.create_index(
        "ix_automations_last_test_run_id", "automations", ["last_test_run_id"]
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
        "COMMENT ON COLUMN automations.draft_body IS "
        "'Partial setup request body for DRAFT automation rows.'"
    )


def downgrade() -> None:
    op.drop_index("ix_automations_last_test_run_id", table_name="automations")
    op.drop_index("ix_automations_source_automation_id", table_name="automations")
    op.drop_index("ix_automations_org_lifecycle_updated_at", table_name="automations")
    with op.batch_alter_table("automations") as batch_op:
        batch_op.drop_constraint(
            "fk_automations_source_automation_id", type_="foreignkey"
        )
        batch_op.drop_column("last_test_run_id")
        batch_op.drop_column("source_automation_id")
        batch_op.drop_column("dispatchable")
        batch_op.drop_column("validation_errors")
        batch_op.drop_column("draft_body")
        batch_op.drop_column("draft_endpoint")
        batch_op.alter_column("entrypoint", existing_type=sa.Text(), nullable=False)
        batch_op.alter_column("tarball_path", existing_type=sa.Text(), nullable=False)
        batch_op.alter_column("trigger", existing_type=sa.JSON(), nullable=False)
        batch_op.alter_column(
            "name", existing_type=sa.String(length=500), nullable=False
        )

    op.drop_index(
        "ix_automation_runs_status_trigger_source", table_name="automation_runs"
    )
    op.drop_index("ix_automation_runs_trigger_source", table_name="automation_runs")
    op.drop_column("automation_runs", "trigger_source")

    op.drop_index("ix_automations_lifecycle_status", table_name="automations")
    op.drop_column("automations", "lifecycle_status")
