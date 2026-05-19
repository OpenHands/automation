"""Add key-value store for automation state persistence.

This migration adds:
1. enable_kv_store column to automations table (opt-in flag)
2. automation_kv table for storing encrypted state document (ONE per automation)

Single-Document Design (Deadlock Prevention)
============================================

Each automation has exactly ONE row in automation_kv containing its entire
state as an encrypted JSON document. The API presents a key-value interface,
but "keys" are top-level fields within this single document.

By storing all state in one row per automation, we eliminate multi-key
deadlock scenarios. All operations serialize through a single row lock.

Storage Design Decisions
========================

Column type: BYTEA (not TEXT or JSONB)
    - We encrypt values with AES-256-GCM at the application layer
    - Encrypted data is raw bytes, not text or valid JSON
    - BYTEA avoids the ~33% overhead of base64 encoding that TEXT would require
    - See automation/utils/kv.py for full encryption design rationale

TOAST strategy: EXTERNAL (not EXTENDED)
    PostgreSQL's TOAST has four storage strategies:
    - PLAIN:    No compression, no out-of-line storage
    - MAIN:     Compress, avoid out-of-line if possible
    - EXTENDED: Compress, then out-of-line if needed (default for BYTEA)
    - EXTERNAL: Out-of-line without compression

    We use EXTERNAL because encrypted data is high-entropy and incompressible.
    The default EXTENDED would waste CPU attempting compression on every write,
    only to give up and store uncompressed anyway. EXTERNAL skips this futility.

Schema comments: COMMENT ON TABLE/COLUMN
    Added for DBAs and database tools that inspect the schema directly.
    Documents the encryption format and storage choices without requiring
    access to application source code.

Revision ID: 005
Revises: 004
Create Date: 2026-04-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "006"
down_revision: str = "005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Add enable_kv_store column to automations table
    op.add_column(
        "automations",
        sa.Column(
            "enable_kv_store", sa.Boolean, nullable=False, server_default="false"
        ),
    )

    # Create automation_kv table - ONE row per automation (single-document design)
    # Note: state_encrypted is BYTEA (LargeBinary) for efficient binary storage.
    # See module docstring for design rationale.
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
        sa.Column("state_encrypted", sa.LargeBinary, nullable=False),
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

    # Set TOAST storage strategy to EXTERNAL for encrypted column.
    # Encrypted data is high-entropy and won't compress, so skip the futile
    # compression attempt that EXTENDED (the default) would perform.
    # EXTERNAL = store out-of-line without compression.
    op.execute(
        "ALTER TABLE automation_kv ALTER COLUMN state_encrypted SET STORAGE EXTERNAL"
    )

    # Add schema-level documentation for the table and columns.
    # This helps DBAs and tools understand the purpose without reading code.
    op.execute(
        "COMMENT ON TABLE automation_kv IS "
        "'Single-document state store for automation persistence. "
        "Each automation has ONE row containing its entire state as encrypted JSON. "
        "The API presents a key-value interface where keys are top-level fields. "
        "Single-row design eliminates multi-key deadlock scenarios. "
        "See automation/utils/kv.py for encryption details.'"
    )
    op.execute(
        "COMMENT ON COLUMN automation_kv.state_encrypted IS "
        "'AES-256-GCM encrypted JSON document containing all KV pairs. "
        "Format: 12-byte nonce || ciphertext || 16-byte auth tag. "
        'Decrypted example: {"config": {...}, "counter": 42, "queue": [...]}. '
        "STORAGE EXTERNAL: skip compression (ciphertext is incompressible).'"
    )


def downgrade() -> None:
    op.drop_index("ix_automation_kv_automation_id", table_name="automation_kv")
    op.drop_table("automation_kv")
    op.drop_column("automations", "enable_kv_store")
