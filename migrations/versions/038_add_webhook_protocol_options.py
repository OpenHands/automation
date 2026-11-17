"""Add optional body delivery IDs, source admission and acknowledgement profiles.

Revision ID: 038
Revises: 037
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "038"
down_revision: str = "037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for name, length in (
        ("event_id_expr", 500),
        ("admission_expr", 2000),
        ("acknowledgement", 50),
    ):
        op.add_column(
            "custom_webhooks", sa.Column(name, sa.String(length), nullable=True)
        )


def downgrade() -> None:
    for name in ("acknowledgement", "admission_expr", "event_id_expr"):
        op.drop_column("custom_webhooks", name)
