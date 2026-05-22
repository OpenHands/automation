"""SQLAlchemy ORM models for the automations service."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
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
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from openhands.automation.utils import utcnow


class WebSocketStatus(enum.Enum):
    """Runtime connection status for an outbound WebSocket source."""

    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    ERROR = "ERROR"


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

    # Optional prompt (set when created via preset endpoints)
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Model profile name to use for automation runs.
    # None is only used for legacy/local fallback.
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Trigger config — for MVP, only cron is supported.
    # Uses generic JSON type for cross-database compatibility (PostgreSQL + SQLite)
    trigger: Mapped[dict] = mapped_column(JSON, nullable=False)

    # Path to SDK code tarball (e.g., S3 or GCS URL)
    tarball_path: Mapped[str] = mapped_column(Text, nullable=False)

    # Relative path inside tarball to setup script (e.g., setup.sh)
    setup_script_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Command to execute the automation (e.g., "uv run script.py")
    entrypoint: Mapped[str] = mapped_column(Text, nullable=False)

    # Maximum execution time in seconds (None = use system default)
    timeout: Mapped[int | None] = mapped_column(nullable=True)

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

    # Conversation created by the SDK script (set by completion callback)
    conversation_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Pre-computed deadline: started_at + max_duration. Set when transitioning
    # to RUNNING, used by the staleness watchdog for efficient indexed queries.
    timeout_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    # If True, sandbox is not deleted after run completes (for debugging)
    keep_alive: Mapped[bool] = mapped_column(default=False, nullable=False)

    # The sandbox ID used for execution (for status verification)
    sandbox_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # The agent-server BashCommand id for this run's dispatched bash chain.
    # Stored so the verifier can filter BashOutput events by this specific
    # command and avoid sampling output from concurrent bash activity on a
    # shared agent server (e.g., the agent's TerminalTool or other runs in
    # local mode). Set immediately after `_start_bash` returns.
    bash_command_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Event payload for event-triggered runs (JSON)
    # Contains the webhook payload that triggered this run.
    # For GitHub events: model_dump() of the parsed Pydantic event
    # For custom webhooks: the raw payload dict
    # Uses generic JSON type for cross-database compatibility (PostgreSQL + SQLite)
    event_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)

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


class CustomWebhook(Base):
    """A custom webhook integration for an organization.

    Note: Built-in integrations (github) don't use this table.
    This is only for custom/generic webhook sources where users configure
    their own webhook URLs and secrets.

    The event_key_expr field specifies a JMESPath expression to extract the
    event identifier from the incoming payload. Examples:
    - "type" -> payload["type"]
    - "event.type" -> payload["event"]["type"]
    - "type || event.name" -> try payload["type"], then payload["event"]["name"]

    The signature_header field specifies which HTTP header contains the HMAC
    signature. Different providers use different headers:
    - Stripe: "Stripe-Signature"
    - Slack: "X-Slack-Signature"
    - Generic: "X-Signature-256" (default)
    """

    __tablename__ = "custom_webhooks"

    # Primary key for the custom webhook record
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    # Organization that owns this webhook integration
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)

    # Human-readable display name (e.g., "Stripe Production", "Slack Alerts")
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Webhook source identifier used in URL routing and trigger matching.
    # Must be unique per org. Forms part of the webhook endpoint URL:
    # POST /v1/events/{org_id}/{source}
    source: Mapped[str] = mapped_column(String(100), nullable=False)

    # Shared secret for HMAC-SHA256 signature verification.
    # The webhook provider signs payloads with this secret; we verify
    # the signature to ensure authenticity and integrity.
    webhook_secret: Mapped[str] = mapped_column(String(255), nullable=False)

    # Whether this webhook integration is active. Disabled webhooks
    # reject incoming events with 404 (as if the source doesn't exist).
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)

    # JMESPath expression to extract the event type identifier from the
    # incoming payload. The extracted value is matched against the trigger's
    # `on` patterns. Default "type" works for many webhooks (e.g., Stripe
    # sends {"type": "payment.completed", ...}). Supports JMESPath
    # alternatives: "type || event.name" tries multiple paths in order.
    event_key_expr: Mapped[str] = mapped_column(
        String(500), nullable=False, default="type"
    )

    # HTTP header name containing the HMAC signature. Different providers
    # use different headers (e.g., Stripe: "Stripe-Signature",
    # Slack: "X-Slack-Signature"). Defaults to "X-Signature-256".
    signature_header: Mapped[str] = mapped_column(
        String(100), nullable=False, default="X-Signature-256"
    )

    # Timestamp when the webhook integration was created
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    # Timestamp of the last update; auto-set on modification
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=utcnow,
        nullable=False,
    )

    __table_args__ = (
        Index("ix_custom_webhooks_org_source", "org_id", "source", unique=True),
    )


class OutboundWebSocketSource(Base):
    """An outbound WebSocket connection that receives events from an external service.

    Unlike CustomWebhook (where the external service connects to us), this model
    represents a connection WE initiate to an external service. A background
    SocketManager maintains the connection and dispatches received events through
    the same trigger-matching pipeline used by webhooks.

    Two kinds are supported, selected via the ``kind`` discriminator column:

    ``"generic"``
        Connects to a static ``wss://`` URL with optional HTTP headers.
        Suitable for any service that exposes a plain WebSocket endpoint.

    ``"slack"``
        Connects to Slack's Socket Mode API.  Requires a Slack App-Level Token
        (``xapp-…``).  The connection URL is fetched dynamically by calling
        ``apps.connections.open`` before each connect attempt; Slack-specific
        envelope ACKs are handled automatically.

    Event routing uses the same JMESPath machinery as webhooks:

    - ``event_key_expr`` extracts the event-type string that is matched against
      ``trigger.on`` patterns in automations (e.g. ``"payload.event.type"``
      yields ``"message"`` for Slack message events).
    - ``payload_expr`` unwraps outer envelopes before the payload is stored on
      the run and handed to ``trigger.filter`` evaluation (e.g. ``"payload.event"``
      for Slack strips the Socket Mode envelope).
    - ``filter_expr`` is a *connection-level* pre-filter: events that do not match
      are silently dropped before any automation matching occurs.  Use this to
      avoid dispatching irrelevant high-volume events (e.g. bot messages).
    """

    __tablename__ = "outbound_websocket_sources"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)

    # Human-readable label
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Slug used as the event ``source`` name in trigger matching and URLs.
    # Must be unique per org (enforced by the unique index below).
    source: Mapped[str] = mapped_column(String(100), nullable=False)

    # Discriminator: "generic" or "slack"
    kind: Mapped[str] = mapped_column(String(50), nullable=False)

    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)

    # --- JMESPath expressions (common to all kinds) ---

    # Extracts the event-type key used for trigger.on pattern matching.
    # Defaults differ per kind and are set at the schema layer:
    #   generic → "type"
    #   slack   → "payload.event.type"
    event_key_expr: Mapped[str] = mapped_column(String(500), nullable=False)

    # Unwraps outer envelopes so the stored/filtered payload is the inner event.
    # None means pass the raw message through unchanged.
    #   slack default → "payload.event"
    payload_expr: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Connection-level pre-filter (JMESPath).  Evaluated against the raw message
    # *before* payload unwrapping.  Events that do not match are dropped silently.
    # None means accept all events.
    filter_expr: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- kind = "generic" fields ---

    # Static wss:// URL to connect to.
    url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # JSON object of HTTP headers to send on the WebSocket upgrade request.
    # Values may contain ${SECRET_NAME} placeholders resolved at connect time
    # from the automation service's secret store (future enhancement; stored
    # verbatim for now — treat as sensitive and encrypt at rest in production).
    headers: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # --- kind = "slack" fields ---

    # Slack App-Level Token (xapp-…).  Required for Socket Mode.
    # Used to call apps.connections.open to obtain a fresh wss:// URL on each
    # connect attempt.  Treat as sensitive; encrypt at rest in production.
    app_token: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # --- Runtime state (managed by SocketManager, not by the API) ---

    status: Mapped[WebSocketStatus] = mapped_column(
        Enum(WebSocketStatus, native_enum=False, length=20),
        nullable=False,
        default=WebSocketStatus.DISCONNECTED,
    )
    status_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    connected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_event_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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

    __table_args__ = (
        Index(
            "ix_outbound_ws_sources_org_source", "org_id", "source", unique=True
        ),
    )
