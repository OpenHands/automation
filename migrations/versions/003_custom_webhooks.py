"""Add custom_webhooks table for storing custom webhook integrations.

Note: Built-in integrations (github, gitlab) don't use this table.
This is only for custom/generic webhook sources where users configure
their own webhook URLs and secrets.

Revision ID: 003
Revises: 002
Create Date: 2026-04-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID


revision: str = "003"
down_revision: str = "002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "custom_webhooks",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("source", sa.String(100), nullable=False),  # user-defined source name
        sa.Column("webhook_secret", sa.String(255), nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default="true"),
        # Dot-notation path to extract event identifier from payload
        # Default "type" works for many webhooks (e.g., Stripe: {"type": "payment.completed"})
        sa.Column(
            "event_type_path",
            sa.String(255),
            nullable=False,
            server_default="type",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index("ix_custom_webhooks_org_id", "custom_webhooks", ["org_id"])
    op.create_index(
        "ix_custom_webhooks_org_source",
        "custom_webhooks",
        ["org_id", "source"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_table("custom_webhooks")
