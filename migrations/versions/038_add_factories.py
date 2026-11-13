"""Add factories: a named group that automations can belong to.

- factories holds the groups.
- automations.factory_id links an automation to at most one factory. It is
  NULL for every existing row, so nothing is backfilled. Deleting a factory
  sets it back to NULL.

Revision ID: 038
Revises: 037
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "038"
down_revision: str = "037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "factories",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    # Batch mode: SQLite cannot add a foreign key with ALTER TABLE.
    with op.batch_alter_table("automations") as batch:
        batch.add_column(sa.Column("factory_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_automations_factory_id",
            "factories",
            ["factory_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index("ix_automations_factory_id", ["factory_id"])


def downgrade() -> None:
    with op.batch_alter_table("automations") as batch:
        batch.drop_index("ix_automations_factory_id")
        batch.drop_constraint("fk_automations_factory_id", type_="foreignkey")
        batch.drop_column("factory_id")
    op.drop_table("factories")
