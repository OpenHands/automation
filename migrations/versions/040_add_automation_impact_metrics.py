"""Store per-run automation impact metric facts.

Revision ID: 040
Revises: 039
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "040"
down_revision: str = "039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "automations",
        sa.Column("impact_metrics", sa.JSON(), nullable=True),
    )
    op.create_table(
        "automation_run_impact_values",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("automation_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("metric_key", sa.String(length=100), nullable=False),
        sa.Column("metric_label", sa.String(length=200), nullable=False),
        sa.Column("expression", sa.Text(), nullable=False),
        sa.Column("value_type", sa.String(length=20), nullable=False),
        sa.Column("value_json", sa.JSON(), nullable=True),
        sa.Column("numeric_value", sa.Float(), nullable=True),
        sa.Column("text_value", sa.Text(), nullable=True),
        sa.Column("boolean_value", sa.Boolean(), nullable=True),
        sa.Column("extraction_error", sa.Text(), nullable=True),
        sa.Column("run_completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["automation_id"], ["automations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["automation_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id", "metric_key", name="uq_automation_run_impact_run_metric"
        ),
    )
    op.create_index(
        "ix_automation_run_impact_automation_metric_completed",
        "automation_run_impact_values",
        ["automation_id", "metric_key", "run_completed_at"],
        unique=False,
    )
    op.create_index(
        "ix_automation_run_impact_org_completed",
        "automation_run_impact_values",
        ["org_id", "run_completed_at"],
        unique=False,
    )
    op.create_index(
        "ix_automation_run_impact_org_automation_completed",
        "automation_run_impact_values",
        ["org_id", "automation_id", "run_completed_at"],
        unique=False,
    )
    where = "numeric_value IS NOT NULL"
    op.create_index(
        "ix_automation_run_impact_numeric",
        "automation_run_impact_values",
        ["automation_id", "metric_key", "run_completed_at", "numeric_value"],
        unique=False,
        postgresql_where=sa.text(where),
        sqlite_where=sa.text(where),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_automation_run_impact_numeric",
        table_name="automation_run_impact_values",
    )
    op.drop_index(
        "ix_automation_run_impact_org_automation_completed",
        table_name="automation_run_impact_values",
    )
    op.drop_index(
        "ix_automation_run_impact_org_completed",
        table_name="automation_run_impact_values",
    )
    op.drop_index(
        "ix_automation_run_impact_automation_metric_completed",
        table_name="automation_run_impact_values",
    )
    op.drop_table("automation_run_impact_values")
    op.drop_column("automations", "impact_metrics")
