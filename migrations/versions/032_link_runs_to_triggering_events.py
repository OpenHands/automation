"""Link event-triggered runs to their source event trace.

Revision ID: 032
Revises: 031
Create Date: 2026-10-02

Store the IntegrationEvent row and serialized parent span context that caused an
event-triggered AutomationRun. The dispatcher uses the persisted span context to
continue the webhook trace when it asynchronously picks up the run.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "032"
down_revision: str = "031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("automations") as batch:
        batch.add_column(
            sa.Column("observability_associations", sa.JSON(), nullable=True)
        )

    with op.batch_alter_table("automation_runs") as batch:
        batch.add_column(sa.Column("trigger_event_id", sa.Uuid(), nullable=True))
        batch.add_column(
            sa.Column("observability_parent_span_context", sa.Text(), nullable=True)
        )
        batch.add_column(
            sa.Column("observability_associations", sa.JSON(), nullable=True)
        )
        batch.create_index(
            "ix_automation_runs_trigger_event_id",
            ["trigger_event_id"],
        )
        batch.create_foreign_key(
            "fk_automation_runs_trigger_event_id_integration_events",
            "integration_events",
            ["trigger_event_id"],
            ["id"],
            ondelete="SET NULL",
        )

    if op.get_bind().dialect.name == "sqlite":
        return

    op.execute(
        "COMMENT ON COLUMN automations.observability_associations "
        "IS 'High-cardinality observability metadata keys mapped to JMESPath "
        "expressions evaluated against incoming event payloads.'"
    )
    op.execute(
        "COMMENT ON COLUMN automation_runs.trigger_event_id "
        "IS 'Integration event row that created this run, for event-triggered runs.'"
    )
    op.execute(
        "COMMENT ON COLUMN automation_runs.observability_parent_span_context "
        "IS 'Serialized Laminar span context from the triggering event, used by "
        "the dispatcher to continue the same trace.'"
    )
    op.execute(
        "COMMENT ON COLUMN automation_runs.observability_associations "
        "IS 'Evaluated high-cardinality observability metadata for this run.'"
    )


def downgrade() -> None:
    with op.batch_alter_table("automation_runs") as batch:
        batch.drop_constraint(
            "fk_automation_runs_trigger_event_id_integration_events",
            type_="foreignkey",
        )
        batch.drop_index("ix_automation_runs_trigger_event_id")
        batch.drop_column("observability_associations")
        batch.drop_column("observability_parent_span_context")
        batch.drop_column("trigger_event_id")

    with op.batch_alter_table("automations") as batch:
        batch.drop_column("observability_associations")
