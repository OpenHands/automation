"""Add service-owned conversation-turn runs.

Revision ID: 026
Revises: 025
"""

import sqlalchemy as sa
from alembic import op


revision = "026"
down_revision = "025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_automation_runs_subject", table_name="automation_runs")
    op.add_column(
        "automation_runs", sa.Column("subject_source", sa.String(100), nullable=True)
    )
    op.add_column(
        "automation_runs", sa.Column("conversation_turn", sa.Text(), nullable=True)
    )
    op.add_column(
        "automation_runs",
        sa.Column("conversation_wake_agent", sa.Boolean(), nullable=True),
    )
    op.execute(
        """
        UPDATE automation_runs
        SET subject_source = (
            SELECT json_extract(automations.trigger, '$.source')
            FROM automations
            WHERE automations.id = automation_runs.automation_id
        )
        WHERE subject_key IS NOT NULL
        """
        if op.get_context().dialect.name == "sqlite"
        else """
        UPDATE automation_runs AS runs
        SET subject_source = automations.trigger ->> 'source'
        FROM automations
        WHERE automations.id = runs.automation_id
          AND runs.subject_key IS NOT NULL
        """
    )
    op.create_index(
        "ix_automation_runs_subject",
        "automation_runs",
        ["automation_id", "subject_source", "subject_key", "created_at"],
        unique=False,
        postgresql_where=sa.text(
            "subject_key IS NOT NULL AND subject_released_at IS NULL"
        ),
        sqlite_where=sa.text("subject_key IS NOT NULL AND subject_released_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_automation_runs_subject", table_name="automation_runs")
    op.drop_column("automation_runs", "conversation_turn")
    op.drop_column("automation_runs", "conversation_wake_agent")
    op.drop_column("automation_runs", "subject_source")
    op.create_index(
        "ix_automation_runs_subject",
        "automation_runs",
        ["automation_id", "subject_key", "created_at"],
        unique=False,
        postgresql_where=sa.text(
            "subject_key IS NOT NULL AND subject_released_at IS NULL"
        ),
        sqlite_where=sa.text("subject_key IS NOT NULL AND subject_released_at IS NULL"),
    )
