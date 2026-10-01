"""Add git sync auth-failure backoff bookkeeping.

Revision ID: 029
Revises: 028
Create Date: 2026-09-30

One column on ``automation_git_sync_org_config`` so the sync loop can back off
and go quiet on a repo it can no longer authenticate to (revoked token,
deleted/private repo) instead of retrying every interval and logging a full
traceback each time: ``consecutive_auth_failures`` drives the exponential
retry backoff and is reset to 0 by a success, a transient failure, or a change
to the repo URL, branch or token.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "029"
down_revision: str = "028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "automation_git_sync_org_config",
        sa.Column(
            "consecutive_auth_failures",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
    )

    if op.get_bind().dialect.name == "sqlite":
        return

    op.execute(
        "COMMENT ON COLUMN automation_git_sync_org_config.consecutive_auth_failures "
        "IS 'Consecutive git-sync cycles that failed authentication/access; reset "
        "to 0 by a success, a transient failure or a repo/token change. Drives "
        "the exponential retry backoff and quiet-after-N logging.'"
    )


def downgrade() -> None:
    op.drop_column("automation_git_sync_org_config", "consecutive_auth_failures")
