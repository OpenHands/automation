"""Add external_conversations: external subject -> conversation mapping.

Revision ID: 020
Revises: 019
Create Date: 2026-08-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "020"
down_revision: str = "019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "external_conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=255), nullable=False),
        sa.Column("subject_key", sa.String(length=500), nullable=False),
        sa.Column("automation_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.String(length=255), nullable=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
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
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["automation_id"], ["automations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["automation_runs.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_external_conversations_subject",
        "external_conversations",
        ["org_id", "source", "subject_key", "automation_id"],
        unique=True,
    )
    op.create_index(
        "ix_external_conversations_run_id",
        "external_conversations",
        ["run_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_external_conversations_run_id", "external_conversations")
    op.drop_index("ix_external_conversations_subject", "external_conversations")
    op.drop_table("external_conversations")
