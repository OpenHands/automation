"""Add signature_scheme to custom_webhooks.

Nullable: existing rows carry no scheme and are read as "hmac_sha256_hex".

Revision ID: 017
Revises: 016
Create Date: 2026-08-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "017"
down_revision: str = "016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    op.add_column(
        "custom_webhooks",
        sa.Column(
            "signature_scheme",
            sa.String(length=50),
            nullable=True,
            server_default="hmac_sha256_hex",
        ),
    )

    if _is_sqlite():
        return

    op.execute(
        "COMMENT ON COLUMN custom_webhooks.signature_scheme IS "
        "'Verifier used for this webhook''s signatures: hmac_sha256_hex "
        "(default, GitHub/Linear style), standard_webhooks "
        "(standardwebhooks.com; GitLab 19.1+, Svix) or slack_v0 (Slack Events "
        "API). NULL means the default.'"
    )


def downgrade() -> None:
    op.drop_column("custom_webhooks", "signature_scheme")
