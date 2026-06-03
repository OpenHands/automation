"""Add CANCELLED status to automation_runs.

Revision ID: 007
Revises: 006
Create Date: 2026-06-03
"""

from collections.abc import Sequence

from alembic import op


revision: str = "007"
down_revision: str = "006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The status column uses native_enum=False (VARCHAR check constraint),
    # so no DDL change is needed — SQLAlchemy stores the value as a plain
    # string and the Python Enum validates on read/write.
    pass


def downgrade() -> None:
    pass
