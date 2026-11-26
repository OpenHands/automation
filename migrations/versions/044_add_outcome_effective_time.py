"""Give outcome facts a time they count at, a dedupe key and dimensions.

Revision ID: 044
Revises: 043
Create Date: 2026-11-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "044"
down_revision: str = "043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "automation_outcome_values"


def upgrade() -> None:
    op.add_column(
        TABLE, sa.Column("effective_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(TABLE, sa.Column("dedupe_key", sa.String(200), nullable=True))
    op.add_column(TABLE, sa.Column("dimensions", sa.JSON(), nullable=True))
    # Facts written before this migration count when they were received.
    op.execute(f"UPDATE {TABLE} SET effective_at = created_at")
    with op.batch_alter_table(TABLE) as batch:
        batch.alter_column(
            "effective_at", existing_type=sa.DateTime(timezone=True), nullable=False
        )
    op.create_index(
        "ix_outcomes_factory_key_effective",
        TABLE,
        ["factory_id", "key", "effective_at"],
    )
    op.create_index(
        "ux_outcomes_dedupe_key",
        TABLE,
        ["factory_id", "key", "dedupe_key"],
        unique=True,
        postgresql_where=sa.text("dedupe_key IS NOT NULL"),
        sqlite_where=sa.text("dedupe_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ux_outcomes_dedupe_key", table_name=TABLE)
    op.drop_index("ix_outcomes_factory_key_effective", table_name=TABLE)
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_column("dimensions")
        batch.drop_column("dedupe_key")
        batch.drop_column("effective_at")
