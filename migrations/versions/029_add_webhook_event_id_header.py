"""Add event_id_header to custom_webhooks.

Optional HTTP header naming the provider's own delivery id, used to drop
redelivered events. Nullable with no server default: existing rows keep the
current behavior (recorded and routed, never deduplicated), and a source opts
in through the normal create/update API.

Revision ID: 029
Revises: 028
Create Date: 2026-09-23
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
        "custom_webhooks",
        sa.Column("event_id_header", sa.String(length=100), nullable=True),
    )

    if _is_sqlite():
        return

    op.execute(
        "COMMENT ON COLUMN custom_webhooks.event_id_header IS "
        "'HTTP header naming this source''s own delivery id, used to drop "
        "redelivered events (e.g. X-GitHub-Delivery). NULL means the source "
        "does not identify its deliveries; its events are recorded and routed "
        "but never deduplicated.'"
    )


def downgrade() -> None:
    op.drop_column("custom_webhooks", "event_id_header")
