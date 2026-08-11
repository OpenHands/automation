"""Add event-based trigger support.

This migration adds:
1. custom_webhooks table for storing custom webhook integrations
   (Note: Built-in integrations like github/gitlab don't use this table)
2. event_payload column to automation_runs for storing trigger payloads
3. signature_header column to custom_webhooks for configurable signature headers

Cross-database compatible: works with both PostgreSQL and SQLite.

Revision ID: 003
Revises: 002
Create Date: 2026-04-06
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import JSON, Boolean, Column, DateTime, String, Uuid, text


revision: str = "003"
down_revision: str = "002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Create custom_webhooks table
    op.create_table(
        "custom_webhooks",
        Column("id", Uuid, primary_key=True),
        Column("org_id", Uuid, nullable=False),
        Column("name", String(255), nullable=False),
        Column("source", String(100), nullable=False),  # user-defined name
        Column("webhook_secret", String(255), nullable=False),
        Column("enabled", Boolean, nullable=False, server_default="true"),
        # JMESPath expression to extract event identifier from payload.
        # Examples: "type", "event.type", "type || event.name"
        # Default "type" for webhooks like Stripe: {"type": "payment.completed"}
        Column(
            "event_key_expr",
            String(500),
            nullable=False,
            server_default="type",
        ),
        # Different webhook providers use different HTTP headers for signatures:
        # - GitHub: X-Hub-Signature-256
        # - Stripe: Stripe-Signature
        # - Slack: X-Slack-Signature
        # - Generic: X-Signature-256 (our default for custom webhooks)
        Column(
            "signature_header",
            String(100),
            nullable=False,
            server_default="X-Signature-256",
        ),
        Column(
            "created_at",
            DateTime(timezone=True),
            nullable=False,
            server_default=text("CURRENT_TIMESTAMP"),
        ),
        Column(
            "updated_at",
            DateTime(timezone=True),
            nullable=False,
            server_default=text("CURRENT_TIMESTAMP"),
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
    # Uses generic JSON for cross-database compatibility
    op.add_column(
        "automation_runs",
        Column("event_payload", JSON, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("automation_runs", "event_payload")
    op.drop_table("custom_webhooks")
