"""Move KV store from one aggregate document to one row per (automation, key).

The previous schema stored an automation's whole state as a single encrypted
JSON document in ``automation_kv`` (unique on ``automation_id``). Because the
API presents independent keys, that model made every ``PUT`` fail once the
combined document crossed the per-value size limit (default 64 KiB) — a few
hundred small, individually valid values would trip it.

This migration fans each legacy document out into per-key rows and adds a small
per-automation metadata row that keeps the single global ``$version`` counter.
It is a one-shot transactional conversion: it decrypts each legacy document,
writes the per-key rows and the metadata row, and only then drops the legacy
table. There is no lazy or dual-read compatibility path afterwards.

Revision ID: 028
Revises: 027
Create Date: 2026-09-25
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "028"
down_revision: str = "027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def _legacy_tables():
    legacy = sa.table(
        "automation_kv_legacy",
        sa.column("automation_id", sa.Uuid),
        sa.column("state_encrypted", sa.Text),
    )
    kv = sa.table(
        "automation_kv",
        sa.column("id", sa.Uuid),
        sa.column("automation_id", sa.Uuid),
        sa.column("key", sa.String),
        sa.column("value_encrypted", sa.Text),
    )
    meta = sa.table(
        "automation_kv_meta",
        sa.column("automation_id", sa.Uuid),
        sa.column("version", sa.BigInteger),
    )
    return legacy, kv, meta


def _migrate_legacy_documents() -> None:
    """Decrypt each legacy aggregate document and fan it into per-key rows."""
    connection = op.get_bind()
    legacy, kv, meta = _legacy_tables()

    rows = connection.execute(
        sa.select(legacy.c.automation_id, legacy.c.state_encrypted)
    ).fetchall()
    if not rows:
        return

    # Decryption needs the deployment's KV secret. Import lazily so the
    # migration module stays importable without the application config, and so
    # an empty database (fresh install) never requires the secret.
    from openhands.automation.config import get_config
    from openhands.automation.utils.kv import decrypt_value, encrypt_value

    secret = get_config().kv.kv_secret
    if not secret:
        raise RuntimeError(
            "AUTOMATION_KV_SECRET is required to migrate existing automation_kv "
            "state to the per-key schema; it cannot be done without decrypting "
            "the legacy documents. Set it and re-run the migration."
        )

    key_rows: list[dict[str, object]] = []
    meta_rows: list[dict[str, object]] = []
    for automation_id, state_encrypted in rows:
        state = decrypt_value(secret, state_encrypted)
        if not isinstance(state, dict):
            raise RuntimeError(
                f"automation_kv state for {automation_id} is not a JSON object; "
                "cannot migrate it to the per-key schema."
            )

        version = state.get("$version", 0)
        if not isinstance(version, int):
            version = 0
        meta_rows.append({"automation_id": automation_id, "version": version})

        for key, value in state.items():
            # ``$version`` now lives on the metadata row; any other reserved
            # key is not something the API could have written.
            if key.startswith("$"):
                continue
            key_rows.append(
                {
                    "id": uuid.uuid4(),
                    "automation_id": automation_id,
                    "key": key,
                    "value_encrypted": encrypt_value(secret, value),
                }
            )

    if meta_rows:
        connection.execute(meta.insert(), meta_rows)
    if key_rows:
        connection.execute(kv.insert(), key_rows)


def upgrade() -> None:
    # Preserve the released single-document table under a temporary name while
    # the new per-key schema is built and populated.
    op.rename_table("automation_kv", "automation_kv_legacy")

    op.create_table(
        "automation_kv",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "automation_id",
            sa.Uuid(),
            sa.ForeignKey("automations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("value_encrypted", sa.Text(), nullable=False),
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
    op.create_index(
        "ix_automation_kv_automation_id_key",
        "automation_kv",
        ["automation_id", "key"],
        unique=True,
    )

    op.create_table(
        "automation_kv_meta",
        sa.Column(
            "automation_id",
            sa.Uuid(),
            sa.ForeignKey("automations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="0"),
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
        sa.PrimaryKeyConstraint("automation_id"),
    )

    _migrate_legacy_documents()

    # Drop the legacy table (and its unique automation_id index) now that every
    # document has been converted.
    op.drop_table("automation_kv_legacy")

    if _is_sqlite():
        return

    op.execute(
        "COMMENT ON TABLE automation_kv IS "
        "'Per-key automation state, one encrypted row per (automation_id, key). "
        "Values scale independently; the per-value size limit is enforced by "
        "the API. See openhands/automation/kv_router.py.'"
    )
    op.execute(
        "COMMENT ON TABLE automation_kv_meta IS "
        "'One row per automation holding the global $version counter used for "
        "optimistic concurrency (if_version) across all of its keys.'"
    )


def downgrade() -> None:
    op.rename_table("automation_kv", "automation_kv_per_key")
    op.rename_table("automation_kv_meta", "automation_kv_meta_new")

    op.create_table(
        "automation_kv",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "automation_id",
            sa.Uuid(),
            sa.ForeignKey("automations.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("state_encrypted", sa.Text(), nullable=False),
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
        "ix_automation_kv_automation_id",
        "automation_kv",
        ["automation_id"],
        unique=True,
    )

    connection = op.get_bind()
    per_key = sa.table(
        "automation_kv_per_key",
        sa.column("automation_id", sa.Uuid),
        sa.column("key", sa.String),
        sa.column("value_encrypted", sa.Text),
    )
    meta = sa.table(
        "automation_kv_meta_new",
        sa.column("automation_id", sa.Uuid),
        sa.column("version", sa.BigInteger),
    )
    aggregate = sa.table(
        "automation_kv",
        sa.column("id", sa.Uuid),
        sa.column("automation_id", sa.Uuid),
        sa.column("state_encrypted", sa.Text),
    )

    from openhands.automation.config import get_config
    from openhands.automation.utils.kv import decrypt_value, encrypt_value

    secret = get_config().kv.kv_secret
    versions = {
        automation_id: version
        for automation_id, version in connection.execute(
            sa.select(meta.c.automation_id, meta.c.version)
        ).fetchall()
    }

    state_by_automation: dict[object, dict[str, object]] = {}
    for automation_id, key, value_encrypted in connection.execute(
        sa.select(per_key.c.automation_id, per_key.c.key, per_key.c.value_encrypted)
    ).fetchall():
        state_by_automation.setdefault(automation_id, {})[key] = decrypt_value(
            secret, value_encrypted
        )

    rows = []
    for automation_id, version in versions.items():
        state = state_by_automation.setdefault(automation_id, {})
        state["$version"] = version
        rows.append(
            {
                "id": uuid.uuid4(),
                "automation_id": automation_id,
                "state_encrypted": encrypt_value(secret, state),
            }
        )
    if rows:
        connection.execute(aggregate.insert(), rows)

    op.drop_table("automation_kv_per_key")
    op.drop_table("automation_kv_meta_new")
