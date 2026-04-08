"""Add event-based trigger support.

This migration adds:
1. custom_webhooks table for storing custom webhook integrations
   (Note: Built-in integrations like github/gitlab don't use this table)
2. event_payload column to automation_runs for storing trigger payloads
3. signature_header column to custom_webhooks for configurable signature headers

Revision ID: 003
Revises: 002
Create Date: 2026-04-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision: str = "003"
down_revision: str = "002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Create custom_webhooks table
    op.create_table(
        "custom_webhooks",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("source", sa.String(100), nullable=False),  # user-defined name
        sa.Column("webhook_secret", sa.String(255), nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default="true"),
        # JMESPath expression to extract event identifier from payload.
        # Examples: "type", "event.type", "type || event.name"
        # Default "type" for webhooks like Stripe: {"type": "payment.completed"}
        sa.Column(
            "event_key_expr",
            sa.String(500),
            nullable=False,
            server_default="type",
        ),
        # Different webhook providers use different HTTP headers for signatures:
        # - GitHub: X-Hub-Signature-256
        # - Stripe: Stripe-Signature
        # - Slack: X-Slack-Signature
        # - Generic: X-Signature-256 (our default for custom webhooks)
        sa.Column(
            "signature_header",
            sa.String(100),
            nullable=False,
            server_default="X-Signature-256",
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

    # 2. Add event_payload column to automation_runs
    # Stores the webhook payload that triggered event-based automation runs.
    # For GitHub events: model_dump() of parsed Pydantic event
    # For custom webhooks: the raw payload dict
    op.add_column(
        "automation_runs",
        sa.Column("event_payload", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("automation_runs", "event_payload")
    op.drop_table("custom_webhooks")
