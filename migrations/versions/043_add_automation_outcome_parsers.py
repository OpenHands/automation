"""Add optional parser rules for nested outcome payloads.

Revision ID: 043
Revises: 042
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "043"
down_revision: str = "042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("automations", sa.Column("outcome_parsers", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("automations", "outcome_parsers")
