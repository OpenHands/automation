"""Store automation outcome value facts.

Revision ID: 042
Revises: 041
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "042"
down_revision: str = "041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "automations",
        sa.Column("outcome_parsers", sa.JSON(), nullable=True),
    )
    op.create_table(
        "automation_outcome_values",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("automation_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("value_type", sa.String(length=20), nullable=False),
        sa.Column("numeric_value", sa.Float(), nullable=True),
        sa.Column("text_value", sa.Text(), nullable=True),
        sa.Column("boolean_value", sa.Boolean(), nullable=True),
        sa.Column("datetime_value", sa.DateTime(timezone=True), nullable=True),
        sa.Column("json_value", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["automation_id"], ["automations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["automation_runs.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_automation_outcome_values_automation_key_created",
        "automation_outcome_values",
        ["automation_id", "key", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_automation_outcome_values_org_created",
        "automation_outcome_values",
        ["org_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_automation_outcome_values_org_automation_created",
        "automation_outcome_values",
        ["org_id", "automation_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_automation_outcome_values_type_created",
        "automation_outcome_values",
        ["automation_id", "key", "value_type", "created_at"],
        unique=False,
    )
    where = "numeric_value IS NOT NULL"
    op.create_index(
        "ix_automation_outcome_values_numeric",
        "automation_outcome_values",
        ["automation_id", "key", "created_at", "numeric_value"],
        unique=False,
        postgresql_where=sa.text(where),
        sqlite_where=sa.text(where),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_automation_outcome_values_numeric",
        table_name="automation_outcome_values",
    )
    op.drop_index(
        "ix_automation_outcome_values_type_created",
        table_name="automation_outcome_values",
    )
    op.drop_index(
        "ix_automation_outcome_values_org_automation_created",
        table_name="automation_outcome_values",
    )
    op.drop_index(
        "ix_automation_outcome_values_org_created",
        table_name="automation_outcome_values",
    )
    op.drop_index(
        "ix_automation_outcome_values_automation_key_created",
        table_name="automation_outcome_values",
    )
    op.drop_table("automation_outcome_values")
    op.drop_column("automations", "outcome_parsers")
