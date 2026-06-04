"""Restore missing indexes on automations and automation_runs

The non-primary-key indexes for these tables are declared on the models and in
migration 001, but are absent in production: alembic reports the latest revision
yet only the PK indexes exist. The most likely cause is that the indexes were
added to migration 001 after it had already been applied, so they were never
created in already-migrated databases (editing an applied migration does not
re-run it). This migration recreates them idempotently.

Effect: removes full table scans for the scheduler/dispatcher/watchdog polls and
the listing queries, notably the dispatcher poll
``WHERE status = 'PENDING' ORDER BY created_at`` (covered by the partial
ix_automation_runs_pending) — one of the heaviest queries on the shared CloudSQL
instance.

Plain CREATE INDEX (not CONCURRENTLY): this migration harness runs on pg8000
inside a transaction holding an advisory lock, where alembic's autocommit_block
(required for CREATE INDEX CONCURRENTLY) fails. Both tables are small in prod
(automation_runs ~52K rows / 16MB, automations ~200 rows), so each build is
sub-second and the brief write lock is acceptable. if_not_exists keeps this a
no-op on databases that already have the indexes (e.g. fresh deployments).

Revision ID: 007
Revises: 006
Create Date: 2026-06-04
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text


revision: str = "007"
down_revision: str | None = "006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # automations
    op.create_index(
        "ix_automations_user_id", "automations", ["user_id"], if_not_exists=True
    )
    op.create_index(
        "ix_automations_org_id", "automations", ["org_id"], if_not_exists=True
    )
    op.create_index(
        "ix_automations_enabled", "automations", ["enabled"], if_not_exists=True
    )
    op.create_index(
        "ix_automations_deleted_at", "automations", ["deleted_at"], if_not_exists=True
    )
    op.create_index(
        "ix_automations_last_polled_at",
        "automations",
        ["last_polled_at"],
        if_not_exists=True,
    )

    # automation_runs
    op.create_index(
        "ix_automation_runs_automation_id",
        "automation_runs",
        ["automation_id"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_automation_runs_status", "automation_runs", ["status"], if_not_exists=True
    )
    op.create_index(
        "ix_automation_runs_pending",
        "automation_runs",
        ["created_at"],
        postgresql_where=text("status = 'PENDING'"),
        if_not_exists=True,
    )
    op.create_index(
        "ix_automation_runs_timeout_at",
        "automation_runs",
        ["timeout_at"],
        if_not_exists=True,
    )


def downgrade() -> None:
    for name, table in [
        ("ix_automation_runs_timeout_at", "automation_runs"),
        ("ix_automation_runs_pending", "automation_runs"),
        ("ix_automation_runs_status", "automation_runs"),
        ("ix_automation_runs_automation_id", "automation_runs"),
        ("ix_automations_last_polled_at", "automations"),
        ("ix_automations_deleted_at", "automations"),
        ("ix_automations_enabled", "automations"),
        ("ix_automations_org_id", "automations"),
        ("ix_automations_user_id", "automations"),
    ]:
        op.drop_index(name, table_name=table, if_exists=True)
