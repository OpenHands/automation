"""Add session-based event routing tables.

This migration adds two tables to support session-based conversation reuse for
event-triggered automations:

1. ``automation_sessions`` — tracks active sessions (sandbox + session key).
   An ACTIVE session routes incoming events to its running sandbox rather than
   creating a new run for each event.

2. ``pending_session_events`` — queues events when a session's sandbox is alive
   or when a sandbox has died and ``on_sandbox_death`` is set to "queue"/"restart".

Cross-database compatible: works with both PostgreSQL and SQLite.

Revision ID: 005
Revises: 004
Create Date: 2026-05-15
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import JSON, Column, DateTime, String, Uuid, text


revision: str = "005"
down_revision: str = "004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Create automation_sessions table
    op.create_table(
        "automation_sessions",
        Column("id", Uuid, primary_key=True),
        Column("automation_id", Uuid, nullable=False),
        Column("session_key", String(255), nullable=False),
        Column("run_id", Uuid, nullable=False),
        # Sandbox identifier (populated by the dispatcher after sandbox creation)
        Column("sandbox_id", String(255), nullable=True),
        # Status: ACTIVE, EXPIRED, or DEAD
        # Using String instead of Enum for cross-database compatibility
        Column("status", String(20), nullable=False, server_default="ACTIVE"),
        Column(
            "started_at",
            DateTime(timezone=True),
            nullable=False,
            server_default=text("CURRENT_TIMESTAMP"),
        ),
        # Pre-computed expiry deadline: started_at + session_timeout_seconds
        Column("expires_at", DateTime(timezone=True), nullable=False),
        Column(
            "last_event_at",
            DateTime(timezone=True),
            nullable=False,
            server_default=text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "ix_automation_sessions_automation_id",
        "automation_sessions",
        ["automation_id"],
    )
    op.create_index(
        "ix_automation_sessions_status",
        "automation_sessions",
        ["status"],
    )
    # Compound index for the primary lookup pattern:
    # SELECT ... WHERE automation_id = ? AND session_key = ? AND status = 'ACTIVE'
    op.create_index(
        "ix_session_lookup",
        "automation_sessions",
        ["automation_id", "session_key", "status"],
    )

    # 2. Create pending_session_events table
    op.create_table(
        "pending_session_events",
        Column("id", Uuid, primary_key=True),
        Column("automation_id", Uuid, nullable=False),
        Column("session_key", String(255), nullable=False),
        # Event payload (same format as automation_runs.event_payload)
        Column("event_payload", JSON, nullable=False),
        Column(
            "created_at",
            DateTime(timezone=True),
            nullable=False,
            server_default=text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "ix_pending_session_events_automation_id",
        "pending_session_events",
        ["automation_id"],
    )
    op.create_index(
        "ix_pending_session_events_session_key",
        "pending_session_events",
        ["session_key"],
    )
    # Compound index for fetching queued events for a specific session
    op.create_index(
        "ix_pending_session_events_lookup",
        "pending_session_events",
        ["automation_id", "session_key"],
    )


def downgrade() -> None:
    op.drop_table("pending_session_events")
    op.drop_table("automation_sessions")
