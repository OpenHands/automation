"""Add automation_git_sync_state table.

Revision ID: 015
Revises: 014
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "015"
down_revision: str = "014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    op.create_table(
        "automation_git_sync_state",
        sa.Column(
            "automation_id",
            sa.Uuid,
            sa.ForeignKey("automations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("slug", sa.String(255), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("last_synced_commit", sa.String(64), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dirty", sa.Boolean, nullable=False, server_default=sa.true()),
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
    )
    op.create_index(
        "ix_automation_git_sync_state_slug",
        "automation_git_sync_state",
        ["slug"],
        unique=True,
    )
    op.create_index(
        "ix_automation_git_sync_state_dirty",
        "automation_git_sync_state",
        ["dirty"],
    )

    if _is_sqlite():
        return

    op.execute(
        "COMMENT ON TABLE automation_git_sync_state IS "
        "'Per-automation git sync bookkeeping: directory slug, last synced "
        "commit/hash, and a dirty flag marking DB-side changes not yet "
        "pushed to git.'"
    )


def downgrade() -> None:
    op.drop_index(
        "ix_automation_git_sync_state_dirty", table_name="automation_git_sync_state"
    )
    op.drop_index(
        "ix_automation_git_sync_state_slug", table_name="automation_git_sync_state"
    )
    op.drop_table("automation_git_sync_state")
