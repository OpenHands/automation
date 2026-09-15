"""Reconcile ORM index declarations with the deployed schema.

Revision ID: 013
Revises: 012
Create Date: 2026-08-04
"""

from collections.abc import Sequence

from alembic import op


revision: str = "013"
down_revision: str = "012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


AUTOMATION_INDEXES = (
    ("ix_automations_user_id", ["user_id"]),
    ("ix_automations_org_id", ["org_id"]),
    ("ix_automations_enabled", ["enabled"]),
    ("ix_automations_deleted_at", ["deleted_at"]),
    ("ix_automations_last_polled_at", ["last_polled_at"]),
)
RUN_TIMEOUT_INDEX = ("ix_automation_runs_timeout_at", ["timeout_at"])


def upgrade() -> None:
    for name, _columns in AUTOMATION_INDEXES:
        op.drop_index(name, table_name="automations", if_exists=True)

    op.drop_index(
        RUN_TIMEOUT_INDEX[0],
        table_name="automation_runs",
        if_exists=True,
    )


def downgrade() -> None:
    for name, columns in AUTOMATION_INDEXES:
        op.create_index(
            name,
            "automations",
            columns,
            if_not_exists=True,
        )

    op.create_index(
        RUN_TIMEOUT_INDEX[0],
        "automation_runs",
        RUN_TIMEOUT_INDEX[1],
        if_not_exists=True,
    )
