"""Add tarball_uploads table for storing upload metadata.

Revision ID: 002
Revises: 001
Create Date: 2026-03-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID


revision: str = "002"
down_revision: str | None = "001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tarball_uploads",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("org_id", UUID(as_uuid=True), nullable=False),
        # User-provided metadata
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        # Upload status
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="UPLOADING",
        ),
        sa.Column("error_message", sa.Text, nullable=True),
        # File metadata
        sa.Column("size_bytes", sa.BigInteger, nullable=True),
        sa.Column("storage_path", sa.Text, nullable=False),
        # Timestamps
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        # Soft delete
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_tarball_uploads_user_id", "tarball_uploads", ["user_id"])
    op.create_index("ix_tarball_uploads_org_id", "tarball_uploads", ["org_id"])
    op.create_index("ix_tarball_uploads_deleted_at", "tarball_uploads", ["deleted_at"])


def downgrade() -> None:
    op.drop_table("tarball_uploads")
