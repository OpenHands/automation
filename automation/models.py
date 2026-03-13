"""SQLAlchemy ORM models for the automations service."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Automation(Base):
    """An automation definition: what to run and when to trigger it."""

    __tablename__ = "automations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(500), nullable=False)

    # Trigger config — for MVP, only cron is supported.
    # Stored as JSON: {"cron": {"schedule": "0 9 * * 5", "timezone": "UTC"}}
    triggers: Mapped[dict] = mapped_column(JSON, nullable=False)

    # S3/GCS path to the SDK code tarball
    sdk_code_tarball_path: Mapped[str] = mapped_column(Text, nullable=False)

    # Encrypted OpenHands API key for V1 API auth
    encrypted_api_key: Mapped[str] = mapped_column(Text, nullable=False)

    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)

    # Last time the scheduler fired this automation
    last_triggered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    __table_args__ = (Index("ix_automations_enabled", "enabled"),)


class AutomationRunStatus:
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class AutomationRun(Base):
    """A single execution of an automation."""

    __tablename__ = "automation_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    automation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=AutomationRunStatus.PENDING,
    )

    # The conversation ID returned by the V1 API (if started successfully)
    conversation_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Trigger source info (e.g., "cron", "manual")
    trigger_type: Mapped[str] = mapped_column(
        String(50), nullable=False, default="cron"
    )

    # Error details if the run failed
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )

    __table_args__ = (
        Index("ix_automation_runs_status", "status"),
        Index("ix_automation_runs_automation_created", "automation_id", "created_at"),
    )
