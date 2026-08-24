"""Add integration_events: one row per accepted delivery.

Gives the service a durable record of every event it accepted, which it has
never had -- until now the only trace of an incoming event was the
``AutomationRun`` rows it happened to create, and an event that matched nothing
left none. Two things follow from the row existing: redeliveries can be
deduplicated across replicas, and "it arrived but matched nothing" becomes
distinguishable from "it never arrived".

The unique index is partial. A NULL ``provider_event_id`` means the provider
does not identify its deliveries, not that the id is unknown, so those rows
must not collide with each other.

Revision ID: 018
Revises: 017
Create Date: 2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "018"
down_revision: str = "017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "integration_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=255), nullable=False),
        sa.Column("provider_event_id", sa.String(length=255), nullable=True),
        sa.Column("event_key", sa.String(length=255), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("matched_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_integration_events_dedupe",
        "integration_events",
        ["org_id", "source", "provider_event_id"],
        unique=True,
        postgresql_where=sa.text("provider_event_id IS NOT NULL"),
        sqlite_where=sa.text("provider_event_id IS NOT NULL"),
    )
    op.create_index(
        "ix_integration_events_received_at",
        "integration_events",
        ["received_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_integration_events_received_at", "integration_events")
    op.drop_index("ix_integration_events_dedupe", "integration_events")
    op.drop_table("integration_events")
