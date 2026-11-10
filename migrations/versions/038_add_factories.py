"""Add factories and link automations to them.

- factories groups automations that serve one goal. Zero factories is valid.
- automations.factory_id is the factory an automation belongs to. Existing
  automations stay null, so nothing needs a backfill.

Revision ID: 038
Revises: 037
Create Date: 2026-11-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "038"
down_revision: str = "037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FK = "fk_automations_factory_id_factories"


def upgrade() -> None:
    op.create_table(
        "factories",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(64), nullable=False),
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
    )
    op.create_index(
        "ix_factories_org_slug", "factories", ["org_id", "slug"], unique=True
    )

    with op.batch_alter_table("automations") as batch:
        batch.add_column(sa.Column("factory_id", sa.Uuid(), nullable=True))
        batch.create_index("ix_automations_factory_id", ["factory_id"])
        batch.create_foreign_key(
            _FK, "factories", ["factory_id"], ["id"], ondelete="SET NULL"
        )


def downgrade() -> None:
    with op.batch_alter_table("automations") as batch:
        batch.drop_constraint(_FK, type_="foreignkey")
        batch.drop_index("ix_automations_factory_id")
        batch.drop_column("factory_id")

    op.drop_index("ix_factories_org_slug", table_name="factories")
    op.drop_table("factories")
