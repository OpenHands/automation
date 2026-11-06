"""Scope git sync to organizations.

Revision ID: 023
Revises: 022
Create Date: 2026-09-05

Git sync used to be service-wide: one runtime-config blob and one set of
status keys in `automation_service_metadata`, and a globally unique directory
slug per synced automation. Each organization now syncs its own automations
to its own repo, so config and bookkeeping move to a per-org table and slugs
are unique per org.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op


revision: str = "023"
down_revision: str = "022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The deterministic local-mode org, as derived by `auth.py`'s
# `_get_local_user()`. Duplicated rather than imported: migrations must not
# load the application.
_LOCAL_ORG_ID = uuid.uuid5(uuid.NAMESPACE_DNS, "openhands-local-org")

_LEGACY_CONFIG_KEY = "git_sync_config_override"
# Legacy service-metadata key -> column on the per-org table.
_LEGACY_STATUS_COLUMNS = {
    "git_sync_last_commit": "last_synced_commit",
    "git_sync_last_path": "last_synced_path",
    "git_sync_last_run_at": "last_run_at",
    "git_sync_last_error": "last_error",
    "git_sync_last_error_at": "last_error_at",
}
_TIMESTAMP_COLUMNS = {"last_run_at", "last_error_at"}

_service_metadata = sa.table(
    "automation_service_metadata",
    sa.column("key", sa.String(255)),
    sa.column("value", sa.Text()),
)
_org_config = sa.table(
    "automation_git_sync_org_config",
    sa.column("org_id", sa.Uuid()),
    sa.column("overrides", sa.Text()),
    sa.column("last_synced_commit", sa.String(64)),
    sa.column("last_synced_path", sa.String(255)),
    sa.column("last_run_at", sa.DateTime(timezone=True)),
    sa.column("last_error", sa.Text()),
    sa.column("last_error_at", sa.DateTime(timezone=True)),
)


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def _parse_timestamp(value: str | None) -> datetime | None:
    # The loop wrote "" to clear a value, so blank means unset.
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def upgrade() -> None:
    op.create_table(
        "automation_git_sync_org_config",
        sa.Column("org_id", sa.Uuid, primary_key=True),
        sa.Column("overrides", sa.Text, nullable=False, server_default="{}"),
        sa.Column("configured_by_user_id", sa.Uuid, nullable=True),
        sa.Column("last_synced_commit", sa.String(64), nullable=True),
        sa.Column("last_synced_path", sa.String(255), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sync_started_at", sa.DateTime(timezone=True), nullable=True),
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

    # Nullable first so existing rows can be backfilled from their automation
    # (the CASCADE foreign key guarantees every row still has one), then made
    # required. Batch mode is what lets SQLite alter the column; on PostgreSQL
    # it is a plain ALTER TABLE.
    op.add_column(
        "automation_git_sync_state", sa.Column("org_id", sa.Uuid, nullable=True)
    )
    op.execute(
        "UPDATE automation_git_sync_state SET org_id = ("
        "SELECT org_id FROM automations "
        "WHERE automations.id = automation_git_sync_state.automation_id)"
    )
    # Before the batch step, so SQLite's table recreate doesn't carry over an
    # index that is about to be replaced.
    op.drop_index(
        "ix_automation_git_sync_state_slug", table_name="automation_git_sync_state"
    )
    with op.batch_alter_table("automation_git_sync_state") as batch:
        batch.alter_column("org_id", existing_type=sa.Uuid(), nullable=False)
    op.create_index(
        "ix_automation_git_sync_state_org_id",
        "automation_git_sync_state",
        ["org_id"],
    )
    op.create_index(
        "ix_automation_git_sync_state_org_slug",
        "automation_git_sync_state",
        ["org_id", "slug"],
        unique=True,
    )

    _move_legacy_service_metadata()

    if _is_sqlite():
        return

    op.execute(
        "COMMENT ON TABLE automation_git_sync_org_config IS "
        "'Per-organization git sync: runtime config overrides (JSON, secrets "
        "wrapped at rest), last synced commit/run/error, and the sync lease.'"
    )


def _move_legacy_service_metadata() -> None:
    """Carry the service-wide config and status over to the local org's row.

    Only a local-mode loop ever ran a cycle, and only a cycle writes the
    status keys, so their presence (or a SQLite database, which is what local
    deployments use) means this deployment is the local org. A PostgreSQL
    database holding only the config blob is a cloud org that saved settings
    while sync was unavailable there: there is no org to attribute it to, so
    it is dropped rather than handed to the local org id.
    """
    bind = op.get_bind()
    keys = [_LEGACY_CONFIG_KEY, *_LEGACY_STATUS_COLUMNS]
    legacy: dict[str, str] = {
        row.key: row.value
        for row in bind.execute(
            sa.select(_service_metadata.c.key, _service_metadata.c.value).where(
                _service_metadata.c.key.in_(keys)
            )
        )
    }
    if not legacy:
        return

    ran_here = _is_sqlite() or any(key in legacy for key in _LEGACY_STATUS_COLUMNS)
    if ran_here:
        values: dict[str, object] = {
            "org_id": _LOCAL_ORG_ID,
            "overrides": legacy.get(_LEGACY_CONFIG_KEY) or "{}",
        }
        for key, column in _LEGACY_STATUS_COLUMNS.items():
            raw = legacy.get(key)
            values[column] = (
                _parse_timestamp(raw) if column in _TIMESTAMP_COLUMNS else raw or None
            )
        bind.execute(_org_config.insert().values(**values))

    bind.execute(_service_metadata.delete().where(_service_metadata.c.key.in_(keys)))


def downgrade() -> None:
    """Best-effort reverse.

    Only the local org's row can go back to service metadata; other orgs'
    config is dropped with the table. Re-creating the global slug index fails
    if two orgs share a slug.
    """
    bind = op.get_bind()
    row = bind.execute(
        sa.select(_org_config).where(_org_config.c.org_id == _LOCAL_ORG_ID)
    ).first()
    if row is not None:
        keys = [_LEGACY_CONFIG_KEY, *_LEGACY_STATUS_COLUMNS]
        bind.execute(
            _service_metadata.delete().where(_service_metadata.c.key.in_(keys))
        )
        legacy: dict[str, str] = {_LEGACY_CONFIG_KEY: row.overrides or "{}"}
        for key, column in _LEGACY_STATUS_COLUMNS.items():
            value = getattr(row, column)
            if value is None:
                continue
            legacy[key] = value.isoformat() if isinstance(value, datetime) else value
        for key, value in legacy.items():
            bind.execute(_service_metadata.insert().values(key=key, value=value))

    op.drop_index(
        "ix_automation_git_sync_state_org_slug",
        table_name="automation_git_sync_state",
    )
    op.drop_index(
        "ix_automation_git_sync_state_org_id", table_name="automation_git_sync_state"
    )
    with op.batch_alter_table("automation_git_sync_state") as batch:
        batch.drop_column("org_id")
    op.create_index(
        "ix_automation_git_sync_state_slug",
        "automation_git_sync_state",
        ["slug"],
        unique=True,
    )
    op.drop_table("automation_git_sync_org_config")
