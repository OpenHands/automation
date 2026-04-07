"""Add signature_header column to custom_webhooks table.

Different webhook providers use different HTTP headers for signatures:
- GitHub: X-Hub-Signature-256
- Stripe: Stripe-Signature
- Slack: X-Slack-Signature
- Generic: X-Signature-256 (our default for custom webhooks)

Revision ID: 005
Revises: 004
Create Date: 2026-04-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "005"
down_revision: str = "004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "custom_webhooks",
        sa.Column(
            "signature_header",
            sa.String(100),
            nullable=False,
            server_default="X-Signature-256",
        ),
    )


def downgrade() -> None:
    op.drop_column("custom_webhooks", "signature_header")
