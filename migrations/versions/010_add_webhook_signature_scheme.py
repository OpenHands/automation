"""Add signature_scheme to custom_webhooks.

Adds a per-webhook signature verification scheme so custom sources can use
schemes other than the default GitHub-style hex HMAC — notably Standard
Webhooks (GitLab 19.1+ signing tokens, Svix).

Revision ID: 010
Revises: 009
Create Date: 2026-07-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "010"
down_revision: str = "009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_sqlite() -> bool:
    """Check if we are running against SQLite (test/dev only)."""
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    op.add_column(
        "custom_webhooks",
        sa.Column(
            "signature_scheme",
            sa.String(length=50),
            nullable=False,
            server_default="hmac_sha256_hex",
        ),
    )

    if not _is_sqlite():
        op.execute(
            "COMMENT ON COLUMN custom_webhooks.signature_scheme IS "
            "'HMAC verification scheme: hmac_sha256_hex (default, GitHub/Linear "
            "style) or standard_webhooks (standardwebhooks.com; GitLab 19.1+ "
            "signing tokens, Svix).'"
        )


def downgrade() -> None:
    op.drop_column("custom_webhooks", "signature_scheme")
