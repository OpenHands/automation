"""SQLAlchemy ORM models for the automations service."""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from openhands.automation.providers import DEFAULT_VERIFIER as DEFAULT_SIGNATURE_SCHEME
from openhands.automation.utils import utcnow


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
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"


class Automation(Base):
    """An automation definition: what to run and when to trigger it."""

    __tablename__ = "automations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    telemetry_distinct_id: Mapped[str | None] = mapped_column(
        String(256), nullable=True
    )

    # Optional prompt (set when created via preset endpoints)
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Preset-specific metadata (populated by preset endpoints; NULL for custom
    # SDK automations).
    # Uses generic JSON type for cross-database compatibility (PostgreSQL + SQLite)
    preset_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)

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

    # If True, the automation service leaves the sandbox for the runtime TTL
    # reaper instead of explicitly deleting it after a terminal run. Null/False
    # means the automation service owns explicit cleanup.
    keep_alive: Mapped[bool | None] = mapped_column(default=None, nullable=True)

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
    telemetry_distinct_id: Mapped[str | None] = mapped_column(
        String(256), nullable=True
    )

    status: Mapped[AutomationRunStatus] = mapped_column(
        Enum(AutomationRunStatus, native_enum=False, length=20),
        nullable=False,
        default=AutomationRunStatus.PENDING,
    )

    # Error details if status is FAILED
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Structured current/last run lifecycle detail. Unlike error_detail, this
    # can describe non-terminal transient infrastructure issues while the run
    # remains PENDING/RUNNING.
    status_detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # Conversation created by the SDK script (set by completion callback)
    conversation_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Accumulated LLM cost in USD (set by completion callback).
    # NULL means "unknown" — e.g. runs that predate cost tracking, or that were
    # force-terminated by the watchdog / cancelled so no callback ever fired.
    cost: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Pre-computed deadline: started_at + max_duration. Set when transitioning
    # to RUNNING, used by the staleness watchdog for efficient indexed queries.
    timeout_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

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

    # Additional metadata captured during run execution.
    # For preset automations this may include the semantic task outcome parsed
    # from the final conversation action.
    run_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)

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
        Index("ix_automation_runs_status_created_at", "status", "created_at"),
        Index("ix_automation_runs_status_timeout_at", "status", "timeout_at"),
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

    # Names a verifier in `providers.VERIFIERS`. Nullable because a PATCH may
    # clear it; NULL reads as the default.
    signature_scheme: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
        default=DEFAULT_SIGNATURE_SCHEME,
        server_default=DEFAULT_SIGNATURE_SCHEME,
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


class IntegrationEvent(Base):
    """One accepted delivery, written in the same transaction as its runs.

    The dedupe key for redeliveries, and the only trace an event that matched
    nothing leaves.
    """

    __tablename__ = "integration_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)

    # Provider slug, matching AutomationRun's trigger source: "github" for a
    # builtin, or the custom webhook's own source name.
    source: Mapped[str] = mapped_column(String(255), nullable=False)

    # The provider's id for this delivery, when the transport can supply one:
    # GitHub's X-GitHub-Delivery, Slack's envelope event_id. NULL for providers
    # and custom webhooks that send none -- those events are still recorded,
    # they are just not deduplicated. See the partial index below.
    provider_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    event_key: Mapped[str] = mapped_column(String(255), nullable=False)

    # The payload trigger filters ran against, kept verbatim so a mismatched
    # JMESPath filter can be evaluated against the real thing after the fact.
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    # How many automations this event started a run for. Zero is the
    # interesting value: the event arrived and matched nothing.
    matched_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    __table_args__ = (
        # Deduplication key. Partial, because a NULL provider_event_id means
        # "this provider does not identify its deliveries" rather than "the id
        # is unknown" -- under a plain unique index every such event past the
        # first would collide with the others.
        #
        # Scoped by org, unlike the sketch in #361: `source` is only unique per
        # org for custom webhooks (see ix_custom_webhooks_org_source), so two
        # orgs each running a webhook they both call "ci" would deduplicate
        # against each other's ids. No provider is weakened by the extra
        # column, since a delivery belongs to exactly one org either way.
        Index(
            "ix_integration_events_dedupe",
            "org_id",
            "source",
            "provider_event_id",
            unique=True,
            postgresql_where=text("provider_event_id IS NOT NULL"),
            sqlite_where=text("provider_event_id IS NOT NULL"),
        ),
        # Drives pruning, which is the only query this phase issues.
        Index("ix_integration_events_received_at", "received_at"),
    )


class ExternalConversation(Base):
    """The correspondence between an external subject and one conversation.

    The service owns this mapping and nothing else about the conversation: the
    agent server owns the conversation itself, its history and its lifecycle.
    That division is the whole point of the design -- storing a row here costs
    one table, where owning conversations would cost an event queue, session
    states and sandbox restart semantics inside every automation sandbox.

    ``conversation_id`` is nullable because the service does not learn it until
    the run that created it completes: the SDK reports it on the completion
    callback. A row with a run but no conversation therefore means "a run is in
    flight for this subject and has not told us its conversation yet", and an
    event arriving in that window creates a run of its own.
    """

    __tablename__ = "external_conversations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)

    # Provider slug, matching the trigger's source: "slack", "github", or a
    # custom webhook's own source name.
    source: Mapped[str] = mapped_column(String(255), nullable=False)

    # The provider's name for the external thing: "T123/C456/1755000000.000100"
    # for a Slack thread, "owner/repo#12" for a pull request.
    subject_key: Mapped[str] = mapped_column(String(500), nullable=False)

    # Scoped per automation, unlike the sketch in #362. A conversation belongs
    # to the automation whose script created it -- its prompt, its tools, its
    # tarball. Two automations in one org both watching #engineering would
    # otherwise share a row, and the second would send its turns into the
    # first's conversation, which cannot answer them.
    automation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("automations.id", ondelete="CASCADE"),
        nullable=False,
    )

    # The agent server's id for the conversation. NULL until a run reports one.
    conversation_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # The run that owns the conversation. Kept because the conversation lives
    # inside that run's sandbox: resolving an agent server to send a turn to
    # starts from the run's sandbox_id.
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("automation_runs.id", ondelete="SET NULL"),
        nullable=True,
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
    # Last event routed to this subject, whether it continued the conversation
    # or started a run. Drives expiry once there is a policy to enforce.
    last_event_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # The arbiter for concurrent events on one subject: two transactions
        # that both miss the lookup race to this index, and one loses.
        Index(
            "ix_external_conversations_subject",
            "org_id",
            "source",
            "subject_key",
            "automation_id",
            unique=True,
        ),
        # The completion callback resolves a mapping from the run it is
        # reporting for.
        Index("ix_external_conversations_run_id", "run_id"),
    )


class AutomationServiceMetadata(Base):
    """Service-level metadata shared by all automation deployment modes."""

    __tablename__ = "automation_service_metadata"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
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


class AutomationKV(Base):
    """Single-document state store for automation persistence.

    Each automation has exactly ONE row containing its entire state as an
    encrypted JSON document. The API presents a key-value interface, but
    "keys" are top-level fields within this single document.

    Single-Document Design (Deadlock Prevention):
        By storing all state in one row per automation, we eliminate multi-key
        deadlock scenarios. All operations on an automation's state serialize
        through a single row lock. There's no possibility of lock ordering
        issues because there's only one lock to acquire.

        Trade-off: Every operation reads/writes the entire state blob. This is
        acceptable because automation state is intended to be small (cursors,
        counters, configs) and access is infrequent (scheduled runs).

    Storage Design:
        We store encrypted state as a Fernet token (URL-safe base64 text)
        produced by the SDK's :class:`Cipher`. See
        ``openhands/automation/utils/kv.py`` for the full encryption rationale.
    """

    __tablename__ = "automation_kv"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    automation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("automations.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,  # ONE row per automation
    )

    # Fernet token (URL-safe base64 text) containing the entire state document
    # as JSON. Produced by openhands.sdk.utils.cipher.Cipher.encrypt and
    # consumed by Cipher.decrypt. The decrypted JSON is a dict where keys are
    # the "KV keys" exposed via the API.
    # Example decrypted: {"config": {...}, "counter": 42, "queue": [...]}
    state_encrypted: Mapped[str] = mapped_column(Text, nullable=False)

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
        # Index for efficient lookup by automation_id (unique constraint
        # is already defined on the column, this ensures index exists)
        Index(
            "ix_automation_kv_automation_id",
            "automation_id",
            unique=True,
        ),
    )


class AutomationGitSyncState(Base):
    """Per-automation git sync bookkeeping, one row per synced automation.

    See ``openhands/automation/git_sync/``. Tracks the repo directory name and
    whether the DB side has changed since it was last written to git.

    ``dirty`` is a plain boolean column, not a JSON field, so the sync loop can
    query ``WHERE dirty = true`` identically on SQLite and PostgreSQL.
    """

    __tablename__ = "automation_git_sync_state"

    automation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("automations.id", ondelete="CASCADE"),
        primary_key=True,
    )

    # Directory name within the sync path, e.g. "automations/{slug}/" in the
    # repo. Stable once assigned.
    slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)

    # SHA-256 of the last-synced content (metadata + tarball files), used to
    # detect no-op sync cycles.
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Git commit SHA this automation was last reconciled against.
    last_synced_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Set on every API create/update/delete, cleared once exported. While
    # dirty, the DB side wins over a conflicting git-side change.
    dirty: Mapped[bool] = mapped_column(default=True, nullable=False, index=True)

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
