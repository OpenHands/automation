"""Add outbound_websocket_sources table.

Stores configuration for outbound WebSocket connections that the automation
service initiates and maintains.  Events received over these connections are
dispatched through the same trigger-matching pipeline as inbound webhooks.

Supports two kinds:
  - "generic": static wss:// URL with optional HTTP headers
  - "slack":   Slack Socket Mode via apps.connections.open (dynamic URL)

Revision ID: 007
Revises: 006
Create Date: 2026-05-21
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import Boolean, Column, DateTime, Enum, JSON, String, Text, Uuid, text


revision: str = "007"
down_revision: str = "006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "outbound_websocket_sources",
        Column("id", Uuid, primary_key=True),
        Column("org_id", Uuid, nullable=False),
        Column("name", String(255), nullable=False),
        Column("source", String(100), nullable=False),
        Column("kind", String(50), nullable=False),
        Column("enabled", Boolean, nullable=False, server_default="true"),
        # JMESPath expressions
        Column("event_key_expr", String(500), nullable=False, server_default="type"),
        Column("payload_expr", String(500), nullable=True),
        Column("filter_expr", Text, nullable=True),
        # generic-kind fields
        Column("url", Text, nullable=True),
        Column("headers", JSON, nullable=True),
        # slack-kind fields
        Column("app_token", String(255), nullable=True),
        # runtime state
        Column(
            "status",
            Enum(
                "CONNECTING",
                "CONNECTED",
                "DISCONNECTED",
                "ERROR",
                name="websocketstatus",
                native_enum=False,
                length=20,
            ),
            nullable=False,
            server_default="DISCONNECTED",
        ),
        Column("status_detail", Text, nullable=True),
        Column("connected_at", DateTime(timezone=True), nullable=True),
        Column("last_event_at", DateTime(timezone=True), nullable=True),
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
    op.create_index(
        "ix_outbound_ws_sources_org_id",
        "outbound_websocket_sources",
        ["org_id"],
    )
    op.create_index(
        "ix_outbound_ws_sources_org_source",
        "outbound_websocket_sources",
        ["org_id", "source"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_table("outbound_websocket_sources")
