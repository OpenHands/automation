"""SQLAlchemy ORM models for the automations service."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from automation.utils import utcnow


class Base(DeclarativeBase):
    pass


class UploadStatus(enum.Enum):
    """Status of a tarball upload."""

    UPLOADING = "UPLOADING"  # Upload in progress
    COMPLETED = "COMPLETED"  # Upload successful
    FAILED = "FAILED"  # Upload failed (e.g., size limit exceeded)


class AutomationRunStatus(enum.Enum):
    """Status of an automation run."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class Automation(Base):
    """An automation definition: what to run and when to trigger it."""

    __tablename__ = "automations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(500), nullable=False)

    # Trigger config — for MVP, only cron is supported.
    triggers: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # Path to SDK code tarball (e.g., S3 or GCS URL)
    tarball_path: Mapped[str] = mapped_column(Text, nullable=False)

    # Relative path inside tarball to setup script (e.g., setup.sh)
    setup_script_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Command to execute the automation (e.g., "uv run script.py")
    entrypoint: Mapped[str] = mapped_column(Text, nullable=False)

    # Whether the automation is enabled (can be triggered)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False, index=True)

    # Soft delete timestamp (NULL = not deleted)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    # Last time the scheduler fired this automation
    last_triggered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Last time the scheduler polled/checked this automation
    last_polled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=utcnow,
        nullable=False,
    )

    # Relationship to runs
    runs: Mapped[list["AutomationRun"]] = relationship(
        "AutomationRun", back_populates="automation", cascade="all, delete-orphan"
    )


class AutomationRun(Base):
    """A single execution of an automation.

    This table doubles as the event queue — the poller picks up PENDING rows
    and dispatches them to SaaS for execution.
    """

    __tablename__ = "automation_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    automation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("automations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    status: Mapped[AutomationRunStatus] = mapped_column(
        Enum(AutomationRunStatus, native_enum=False, length=20),
        nullable=False,
        default=AutomationRunStatus.PENDING,
    )

    # Error details if status is FAILED
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationship back to automation
    automation: Mapped["Automation"] = relationship("Automation", back_populates="runs")

    __table_args__ = (
        # Partial index for efficient PENDING polling.
        # This service uses PostgreSQL exclusively in all environments.
        Index(
            "ix_automation_runs_pending",
            "created_at",
            postgresql_where=(status == AutomationRunStatus.PENDING),
        ),
        Index("ix_automation_runs_status", "status"),
    )


class TarballUpload(Base):
    """A tarball upload for automation code.

    Stores metadata about uploaded tarballs. The actual file content
    is stored in GCS at the path specified in storage_path.
    """

    __tablename__ = "tarball_uploads"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)

    # User-provided metadata
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Upload status
    status: Mapped[UploadStatus] = mapped_column(
        Enum(UploadStatus, native_enum=False, length=20),
        nullable=False,
        default=UploadStatus.UPLOADING,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # File metadata
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=utcnow,
        nullable=False,
    )

    # Soft delete
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
