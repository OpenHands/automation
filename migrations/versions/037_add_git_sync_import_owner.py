"""Add automation_git_sync_org_config.import_owner_user_id.

Revision ID: 037
Revises: 036
Create Date: 2026-10-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "037"
down_revision: str = "036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "automation_git_sync_org_config",
        sa.Column("import_owner_user_id", sa.Uuid(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("automation_git_sync_org_config", "import_owner_user_id")
