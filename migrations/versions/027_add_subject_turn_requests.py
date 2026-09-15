"""Add idempotent subject-turn requests.

Revision ID: 027
Revises: 026
"""

import sqlalchemy as sa
from alembic import op


revision = "027"
down_revision = "026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "automation_subject_turns",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("automation_id", sa.Uuid(), nullable=False),
        sa.Column("requester_run_id", sa.Uuid(), nullable=False),
        sa.Column("subject_run_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(100), nullable=False),
        sa.Column("subject_key", sa.String(500), nullable=False),
        sa.Column("idempotency_key", sa.String(500), nullable=False),
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
            ["requester_run_id"], ["automation_runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["subject_run_id"], ["automation_runs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "automation_id",
            "source",
            "subject_key",
            "idempotency_key",
            name="uq_automation_subject_turn_idempotency",
        ),
    )


def downgrade() -> None:
    op.drop_table("automation_subject_turns")
