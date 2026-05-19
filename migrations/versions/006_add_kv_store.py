"""Add key-value store for automation state persistence.

This migration adds the ``automation_kv`` table — one row per automation
holding the entire state document as an encrypted blob.

Single-Document Design (Deadlock Prevention)
============================================

Each automation has exactly ONE row in automation_kv containing its entire
state as an encrypted JSON document. The API presents a key-value interface,
but "keys" are top-level fields within this single document.

By storing all state in one row per automation, we eliminate multi-key
deadlock scenarios. All operations serialize through a single row lock.

Storage Design
==============

The encrypted state is stored as TEXT (a Fernet token, URL-safe base64).
We use the SDK's ``openhands.sdk.utils.cipher.Cipher`` (Fernet under the
hood) for encryption — see ``openhands/automation/utils/kv.py``. Fernet
emits a base64 string rather than raw bytes, so TEXT is the natural column
type. The ~33% base64 overhead is acceptable for the small JSON documents
typical of automation state (counters, cursors, configs) and keeps the
schema simple.

Revision ID: 006
Revises: 005
Create Date: 2026-04-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "006"
down_revision: str = "005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_sqlite() -> bool:
    """Check if we are running against SQLite (test/dev only)."""
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    # Create automation_kv table - ONE row per automation (single-document design)
    # state_encrypted is a Fernet token (URL-safe base64 text) produced by the
    # SDK Cipher. See module docstring for the design rationale.
    op.create_table(
        "automation_kv",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column(
            "automation_id",
            sa.Uuid,
            sa.ForeignKey("automations.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,  # ONE row per automation - critical for deadlock prevention
        ),
        sa.Column("state_encrypted", sa.Text, nullable=False),
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

    # Create unique index on automation_id (enforces one row per automation)
    op.create_index(
        "ix_automation_kv_automation_id",
        "automation_kv",
        ["automation_id"],
        unique=True,
    )

    # Add schema-level documentation for DBAs and tools that inspect the
    # schema directly without reading application source. SQLite doesn't
    # support COMMENT, so skip these statements there.
    if _is_sqlite():
        return

    op.execute(
        "COMMENT ON TABLE automation_kv IS "
        "'Single-document state store for automation persistence. "
        "Each automation has ONE row containing its entire state as encrypted JSON. "
        "The API presents a key-value interface where keys are top-level fields. "
        "Single-row design eliminates multi-key deadlock scenarios. "
        "See openhands/automation/utils/kv.py for encryption details.'"
    )
    op.execute(
        "COMMENT ON COLUMN automation_kv.state_encrypted IS "
        "'Fernet token (URL-safe base64 text) containing the encrypted state "
        "document as JSON. Produced by openhands.sdk.utils.cipher.Cipher.'"
    )


def downgrade() -> None:
    op.drop_index("ix_automation_kv_automation_id", table_name="automation_kv")
    op.drop_table("automation_kv")
