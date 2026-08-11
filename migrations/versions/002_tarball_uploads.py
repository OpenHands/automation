"""Add tarball_uploads table for storing upload metadata.

Cross-database compatible: works with both PostgreSQL and SQLite.

Revision ID: 002
Revises: 001
Create Date: 2026-03-20
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import BigInteger, Column, DateTime, String, Text, Uuid, text


revision: str = "002"
down_revision: str | None = "001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tarball_uploads",
        Column("id", Uuid, primary_key=True),
        Column("user_id", Uuid, nullable=False),
        Column("org_id", Uuid, nullable=False),
        # User-provided metadata
        Column("name", String(255), nullable=False),
        Column("description", Text, nullable=True),
        # Upload status
        Column(
            "status",
            String(20),
            nullable=False,
            server_default="UPLOADING",
        ),
        Column("error_message", Text, nullable=True),
        # File metadata
        Column("size_bytes", BigInteger, nullable=True),
        Column("storage_path", Text, nullable=False),
        # Timestamps
        Column(
            "created_at",
            DateTime(timezone=True),
            nullable=False,
            server_default=text("CURRENT_TIMESTAMP"),
        ),
        Column(
            "updated_at",
            DateTime(timezone=True),
            nullable=False,
            server_default=text("CURRENT_TIMESTAMP"),
        ),
        # Soft delete
        Column("deleted_at", DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_tarball_uploads_user_id", "tarball_uploads", ["user_id"])
    op.create_index("ix_tarball_uploads_org_id", "tarball_uploads", ["org_id"])
    op.create_index("ix_tarball_uploads_deleted_at", "tarball_uploads", ["deleted_at"])


def downgrade() -> None:
    op.drop_table("tarball_uploads")
