"""Add git sync failure backoff bookkeeping.

Revision ID: 029
Revises: 028
Create Date: 2026-09-30

Two columns on ``automation_git_sync_org_config`` so the sync loop can back off
and go quiet on a persistently failing repo (revoked token, deleted/private
repo) instead of retrying every interval and logging a full traceback each
time: ``consecutive_failures`` (reset to 0 on the next success) drives the
exponential retry backoff, and ``last_error_kind`` records whether the last
failure was an auth failure or a transient one.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "029"
down_revision: str = "028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    op.add_column(
        "automation_git_sync_org_config",
        sa.Column(
            "consecutive_failures",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "automation_git_sync_org_config",
        sa.Column("last_error_kind", sa.String(16), nullable=True),
    )

    if _is_sqlite():
        return

    op.execute(
        "COMMENT ON COLUMN automation_git_sync_org_config.consecutive_failures IS "
        "'Consecutive failed git-sync cycles, reset to 0 on success; drives the "
        "exponential retry backoff and quiet-after-N-auth-failures logging.'"
    )
    op.execute(
        "COMMENT ON COLUMN automation_git_sync_org_config.last_error_kind IS "
        "'Kind of the last git-sync failure: auth, transient, or NULL when the "
        "last cycle succeeded.'"
    )


def downgrade() -> None:
    op.drop_column("automation_git_sync_org_config", "last_error_kind")
    op.drop_column("automation_git_sync_org_config", "consecutive_failures")
