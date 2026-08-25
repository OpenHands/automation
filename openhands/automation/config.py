"""Application configuration loaded from environment variables.

This module centralizes all environment variable configuration for the automation
service. Configuration is organized into a composed AppConfig with typed sections:

    AppConfig
    ├── service: ServiceSettings    # Core service (AUTOMATION_ prefix)
    ├── storage: StorageSettings    # File storage (no prefix, SDK conventions)
    ├── log: LogSettings            # Logging (no prefix)
    ├── http: HttpSettings          # HTTP client (AUTOMATION_ prefix)
    ├── sandbox: SandboxSettings    # Sandbox execution (AUTOMATION_ prefix)
    └── kv: KVSettings              # Key-value store (AUTOMATION_ prefix)

Usage (preferred):
    from openhands.automation.config import get_config

    config = get_config()
    config.service.db_host
    config.storage.file_store
    config.log.log_level

Legacy usage (backward compatible, emits deprecation warnings):
    from openhands.automation.config import (
        get_settings, get_storage_settings, get_log_settings
    )

    settings = get_settings()        # Returns config.service
    storage = get_storage_settings() # Returns config.storage
    log = get_log_settings()         # Returns config.log

WARNING: FROZEN CONFIG VALUES
-----------------------------
Some configuration values are read at module import time and frozen for the
process lifetime. These cannot be changed at runtime even if you call
clear_config_cache():

- Retry decorators (auth.py, execution.py): tenacity retry/backoff settings
- Logging configuration (logger.py): log level, format settings

This design is intentional for performance - these values are used in hot paths
where repeated config lookups would add overhead. If you need to test with
different values, use monkeypatching or reload the affected modules.
"""

import os
import uuid
import warnings
from functools import cached_property, lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings


# ---------------------------------------------------------------------------
# LogSettings - Logging configuration
# ---------------------------------------------------------------------------


class LogSettings(BaseSettings):
    """Logging configuration.

    Environment variables (no prefix):
        LOG_JSON: Output JSON logs (default: "1" = enabled)
        LOG_LEVEL: Root log level (default: "INFO")
        AUTOMATION_LOG_LEVEL: Automation-specific log level (default: LOG_LEVEL)
        DEBUG: Enable debug mode, overrides log levels (default: "False")
        LOG_JSON_FOR_CONSOLE: Pretty-print JSON for console (default: "0")
    """

    log_json: bool = True
    log_level: str = "INFO"
    automation_log_level: str | None = None  # Falls back to log_level
    debug: bool = False
    log_json_for_console: bool = False

    model_config = {"env_prefix": ""}

    @property
    def effective_log_level(self) -> str:
        """Get the effective log level, accounting for DEBUG override."""
        if self.debug:
            return "DEBUG"
        return self.log_level

    @property
    def effective_automation_log_level(self) -> str:
        """Get the effective automation log level, accounting for DEBUG override."""
        if self.debug:
            return "DEBUG"
        return self.automation_log_level or self.log_level


# ---------------------------------------------------------------------------
# StorageSettings - File storage backend configuration
# ---------------------------------------------------------------------------


class StorageSettings(BaseSettings):
    """File storage backend configuration.

    The automation service supports three storage backends:
    - Local (local filesystem) - default, for self-hosted deployments
    - GCS (Google Cloud Storage)
    - S3 (AWS S3 or S3-compatible like MinIO)

    Environment variables (no prefix, follows SDK conventions):
        FILE_STORE: Backend type, "local", "gcs", or "s3" (default: "local")

        # GCS settings
        GCS_BUCKET_NAME: GCS bucket name (required if FILE_STORE=gcs)
        STORAGE_EMULATOR_HOST: Fake-gcs-server URL for local dev (optional)

        # S3 settings
        AWS_S3_BUCKET: S3 bucket name (required if FILE_STORE=s3)
        AWS_S3_ENDPOINT: Custom endpoint for MinIO/LocalStack (optional)
        AWS_S3_SECURE: Use HTTPS (default: "true")
        AWS_S3_AUTO_CREATE_BUCKET: Auto-create bucket if missing (default: "false")

        # Local settings
        LOCAL_STORAGE_PATH: Base directory for local storage

        # Size limits
        MAX_UPLOAD_SIZE: Max tarball upload size in bytes (default: 1MB)
        MAX_STREAM_SIZE: Max streaming upload size in bytes (default: 100MB)

        # AWS credentials (read directly by boto3, not validated here)
        AWS_ACCESS_KEY_ID: AWS access key
        AWS_SECRET_ACCESS_KEY: AWS secret key
    """

    file_store: Literal["local", "gcs", "s3"] = "local"

    # GCS settings
    gcs_bucket_name: str | None = None
    storage_emulator_host: str | None = None

    # S3 settings
    aws_s3_bucket: str | None = None
    aws_s3_endpoint: str | None = None
    aws_s3_secure: bool = True
    aws_s3_auto_create_bucket: bool = False

    # Local settings
    local_storage_path: Path = Path("~/.openhands/automation/storage")

    # Size limits
    max_upload_size: int = 1 * 1024 * 1024  # 1 MB
    max_stream_size: int = 100 * 1024 * 1024  # 100 MB

    model_config = {"env_prefix": ""}

    @model_validator(mode="after")
    def validate_bucket_for_backend(self) -> "StorageSettings":
        """Ensure the appropriate bucket/path is configured for the selected backend."""
        if self.file_store == "gcs" and not self.gcs_bucket_name:
            raise ValueError("GCS_BUCKET_NAME is required when FILE_STORE=gcs")
        if self.file_store == "s3" and not self.aws_s3_bucket:
            raise ValueError("AWS_S3_BUCKET is required when FILE_STORE=s3")
        if self.file_store == "local" and not str(self.local_storage_path):
            raise ValueError("LOCAL_STORAGE_PATH is required when FILE_STORE=local")
        return self


# ---------------------------------------------------------------------------
# HttpSettings - HTTP client configuration
# ---------------------------------------------------------------------------


class HttpSettings(BaseSettings):
    """HTTP client configuration for outbound requests.

    Environment variables (AUTOMATION_ prefix):
        AUTOMATION_HTTP_TIMEOUT: Default timeout for HTTP requests (default: 10.0)
        AUTOMATION_HTTP_LONG_TIMEOUT: Timeout for long operations (default: 60.0)
        AUTOMATION_AUTH_CACHE_TTL: Auth token cache TTL in seconds (default: 20.0)
        AUTOMATION_AUTH_CACHE_SIZE: Max entries in auth cache (default: 1024)
        AUTOMATION_AUTH_MAX_RETRIES: Max auth retry attempts (default: 3)
        AUTOMATION_AUTH_INITIAL_BACKOFF: Initial backoff in seconds (default: 1.0)
        AUTOMATION_AUTH_MAX_BACKOFF: Max backoff for retries in seconds (default: 10.0)
    """

    http_timeout: float = 10.0
    http_long_timeout: float = 60.0
    auth_cache_ttl: float = 20.0
    auth_cache_size: int = 1024
    auth_max_retries: int = 3
    auth_initial_backoff: float = 1.0
    auth_max_backoff: float = 10.0

    model_config = {"env_prefix": "AUTOMATION_"}


# ---------------------------------------------------------------------------
# SandboxSettings - Sandbox execution configuration
# ---------------------------------------------------------------------------


class SandboxSettings(BaseSettings):
    """Sandbox execution configuration.

    Environment variables (AUTOMATION_ prefix):
        AUTOMATION_DEFAULT_RUN_DURATION: Default run time in seconds (default: 600)
        AUTOMATION_MAX_RUN_DURATION: Max user-configurable run time in seconds
            (default: 1800)
        AUTOMATION_RUN_TIMEOUT_MARGIN: Slack added to watchdog deadlines in
            seconds (default: 120)
        AUTOMATION_RUN_TIMEOUT_HARD_GRACE: Extra grace before a still-running
            verification result becomes terminal in seconds (default: 600)
        AUTOMATION_SANDBOX_POLL_INTERVAL: Status check interval (default: 5)
        AUTOMATION_SANDBOX_READY_TIMEOUT: Max wait for ready (default: 300)
        AUTOMATION_EXTERNAL_DOWNLOAD_TIMEOUT: Download timeout (default: 120)
        AUTOMATION_EXTERNAL_MAX_FILESIZE: Max tarball size (default: 100MB)
        AUTOMATION_RATE_LIMIT_MIN_WAIT: Initial 429 wait (default: 10)
        AUTOMATION_RATE_LIMIT_MAX_WAIT: Max retry wait (default: 60)
        AUTOMATION_RATE_LIMIT_MAX_RETRIES: Max retries (default: 5)
    """

    default_run_duration: int = 10 * 60  # 10 minutes
    max_run_duration: int = 30 * 60  # 30 minutes
    # Watchdog-deadline slack: covers the in-sandbox post-conversation tail
    # (event settle + stats + close + callback POST) plus one watchdog scan
    # of skew, so the bash service's own timeout always fires first.
    run_timeout_margin: int = 120
    # Bound on deferring "command still running" verification results past
    # the theoretical worst case; only matters if the agent-server's own
    # bash-timeout enforcement is broken.
    run_timeout_hard_grace: int = 600
    sandbox_poll_interval: int = 5
    sandbox_ready_timeout: int = 300
    external_download_timeout: int = 120
    external_max_filesize: int = 100 * 1024 * 1024  # 100 MB
    rate_limit_min_wait: int = 10
    rate_limit_max_wait: int = 60
    rate_limit_max_retries: int = 5

    model_config = {"env_prefix": "AUTOMATION_"}


# ---------------------------------------------------------------------------
# KVSettings - Key-value store configuration
# ---------------------------------------------------------------------------


class KVSettings(BaseSettings):
    """Key-value store configuration for automation state persistence.

    The KV store provides per-automation state storage with encryption and
    JWT-based authentication. It is available to every automation whenever
    AUTOMATION_KV_SECRET is configured at the service level.

    Environment variables (AUTOMATION_ prefix):
        AUTOMATION_KV_SECRET: Secret for JWT signing and value encryption.
            Must be set to enable KV store. Generate with:
            python -c "import secrets; print(secrets.token_urlsafe(32))"
        AUTOMATION_KV_MAX_VALUE_SIZE: Max value size in bytes (default: 64KB)
        AUTOMATION_KV_LOCK_TIMEOUT_MS: Row-lock timeout in ms (default: 5000)
    """

    # Secret key for signing KV store JWT tokens and encrypting KV values.
    # Must be set to enable the KV store feature.
    kv_secret: str = ""

    # Row-lock timeout in milliseconds for KV operations.
    # Applied via PostgreSQL `SET LOCAL lock_timeout` before FOR UPDATE.
    # If the lock isn't acquired within this window we return 409 Conflict
    # with Retry-After so clients can back off and retry.
    kv_lock_timeout_ms: int = 5000

    # Maximum size in bytes for KV store values (plaintext JSON, before encryption).
    #
    # Performance guidance - PostgreSQL TOAST behavior:
    #
    #   Limit     Stored Size   TOAST Chunks   Read Latency
    #   -------   -----------   ------------   ------------
    #   < 2 KB    inline        0              1x (optimal)
    #   2-8 KB    compressed    0              ~2x
    #   64 KB     ~65 KB        ~33            ~5-10x
    #   128 KB    ~131 KB       ~66            ~10-15x
    #   256 KB    ~262 KB       ~131           ~15-25x
    #   512 KB    ~524 KB       ~262           ~25-40x
    #
    # Values > 8KB are stored in a separate TOAST table, requiring index lookups
    # for each ~2KB chunk. The default 64KB is generous for typical KV use cases
    # (counters, flags, small configs). For larger blobs, consider object storage.
    #
    # Set to 0 to disable the limit (not recommended).
    kv_max_value_size: int = 64 * 1024  # 64 KB

    model_config = {"env_prefix": "AUTOMATION_"}

    @property
    def enabled(self) -> bool:
        """Check if KV store is enabled (kv_secret is set)."""
        return bool(self.kv_secret)


# ---------------------------------------------------------------------------
# GitSyncSettings - Git sync configuration
# ---------------------------------------------------------------------------


def normalize_git_sync_path(path: str) -> str:
    """Normalize a repo-relative sync path, rejecting anything that escapes it.

    The path is joined onto the checkout directory, then `shutil.rmtree`'d per
    automation on export and passed to `git add -- <path>` on push, so it must
    stay inside the repo:

    - `..` is rejected: the path is settable at runtime, so a traversing value
      would point `sync_root` at an arbitrary host directory and delete any
      subdirectory there matching an automation slug.
    - Leading slashes are stripped, not rejected, so a mistyped "/automations"
      stays repo-relative (`Path("/repo") / "/etc"` is `/etc` -- pathlib drops
      the left side when the right is absolute).
    - Trailing slashes are stripped because `_changed_slugs_since` matches an
      `f"{sync_path}/"` prefix; "automations/" would match nothing and
      silently mute every import.
    - An empty result is rejected: `git add -A -- ""` is not a valid pathspec
      and would wedge every cycle.
    """
    # Backslashes aren't separators on the platforms this runs on, but a
    # Windows-style value pasted into the UI shouldn't smuggle a traversal
    # segment past the "/"-based split below.
    segments = [
        segment
        for segment in path.strip().replace("\\", "/").split("/")
        if segment and segment != "."
    ]
    if any(segment == ".." for segment in segments):
        raise ValueError(
            f"git sync path {path!r} must stay inside the repository (no '..' segments)"
        )
    if not segments:
        raise ValueError("git sync path must not be empty")
    return "/".join(segments)


class GitSyncSettings(BaseSettings):
    """Git sync configuration for backing up/versioning automations in git.

    When enabled, automations are serialized to files and pushed to a git repo,
    and changes pushed there (e.g. via a PR) are pulled back. Local mode only:
    one repo maps to one agent server, which doesn't fit multi-tenant SaaS.

    Configuring a repo is what turns sync on: there is no separate enable
    flag, so nothing syncs until a repo URL is set here or from the UI.

    Environment variables (AUTOMATION_ prefix):
        AUTOMATION_GIT_SYNC_REPO_URL: Git repo URL to sync to, e.g.
            https://github.com/org/repo.git. Setting it enables sync; empty
            (the default) leaves it off.
        AUTOMATION_GIT_SYNC_BRANCH: Branch to sync (default: "main").
        AUTOMATION_GIT_SYNC_PATH: Directory within the repo automations are
            stored under, no leading/trailing slash (default: "automations").
        AUTOMATION_GIT_SYNC_TOKEN: PAT (or other bearer token) for HTTPS
            authentication against the repo. Passed per git-invocation via
            `-c http.extraHeader`, never written to disk or the remote URL.
        AUTOMATION_GIT_SYNC_ENCRYPTION_KEY: When set, encrypts file contents
            (via the SDK's Fernet-based Cipher, same primitive as the KV
            store) before they're written to the synced repo. Empty disables
            encryption; existing plaintext files remain readable either way.
        AUTOMATION_GIT_SYNC_AUTHOR_NAME: Commit author name (default:
            "OpenHands Automation").
        AUTOMATION_GIT_SYNC_AUTHOR_EMAIL: Commit author email (default:
            "automation@openhands.dev").
        AUTOMATION_GIT_SYNC_LOCAL_WORKDIR: Local working directory for the
            clone. Defaults to "{workspace_base}/git-sync" when empty.
        AUTOMATION_GIT_SYNC_GIT_TIMEOUT_SECONDS: Timeout for individual git
            subprocess invocations (default: 60).
    """

    # The sync interval is deliberately not here: it is runtime-only, set from
    # the UI and stored with the other overrides. See config_override.py.
    #
    # `git_sync_enabled` is the pause switch, not a feature flag: it defaults
    # to on and only ever goes false through a runtime override, so a
    # deployment enables sync by configuring a repo rather than by setting a
    # second thing that has to agree with the first.
    git_sync_enabled: bool = True
    git_sync_repo_url: str = ""
    git_sync_branch: str = "main"
    git_sync_path: str = "automations"
    git_sync_token: str = ""
    git_sync_encryption_key: str = ""
    git_sync_author_name: str = "OpenHands Automation"
    git_sync_author_email: str = "automation@openhands.dev"
    git_sync_local_workdir: str = ""
    git_sync_git_timeout_seconds: float = 60.0

    model_config = {"env_prefix": "AUTOMATION_"}

    @property
    def enabled(self) -> bool:
        """Whether git sync is on: a repo is configured and it isn't paused.

        Doesn't raise when misconfigured -- this section is constructed
        eagerly regardless of deployment mode, so raising would crash every
        deployment on a bad env var. app.py warns once it knows the mode.
        """
        return bool(self.git_sync_repo_url and self.git_sync_enabled)


class SlackAppSettings(BaseModel):
    """One Slack app this deployment holds a Socket Mode connection for.

    `team_id` and `bot_user_id` are asserted against `auth.test` before the
    socket opens, so a mis-pasted token fails loudly instead of silently
    bridging the wrong workspace into this organization.
    """

    org_id: uuid.UUID
    # App-level token (xapp-), which opens the socket.
    app_token: str
    # Bot token (xoxb-), used only to assert identity at startup.
    bot_token: str
    team_id: str
    bot_user_id: str


class StreamSettings(BaseSettings):
    """Stream sources: long-lived inbound connections, supervised in-process.

    Off by default, and a self-hosted capability for two independent reasons:
    Slack does not allow Socket Mode apps in the public Marketplace, and
    connection-scoped state does not fit a stateless autoscaled tier. Webhooks
    remain the cloud path.

    Environment variables (AUTOMATION_ prefix):
        AUTOMATION_STREAMS_ENABLED: Master switch (default: false). With it
            off the supervisor never starts and nothing else changes.
        AUTOMATION_SLACK_APPS: JSON list of Slack apps to connect, each
            {"org_id", "app_token", "bot_token", "team_id", "bot_user_id"}.
        AUTOMATION_STREAM_BACKOFF_SECONDS: Delay before the first restart of
            a failed source, doubled per consecutive failure (default: 5).
        AUTOMATION_STREAM_MAX_BACKOFF_SECONDS: Ceiling for that delay
            (default: 300).
    """

    streams_enabled: bool = False
    slack_apps: list[SlackAppSettings] = Field(default_factory=list)
    stream_backoff_seconds: float = 5.0
    stream_max_backoff_seconds: float = 300.0

    model_config = {"env_prefix": "AUTOMATION_"}

    @property
    def enabled(self) -> bool:
        """Whether to start the supervisor: switched on, and something to run."""
        return bool(self.streams_enabled and self.slack_apps)


# ---------------------------------------------------------------------------
# ServiceSettings - Core service configuration (formerly "Settings")
# ---------------------------------------------------------------------------


class ServiceSettings(BaseSettings):
    """Core service configuration.

    Environment variables (AUTOMATION_ prefix):
        # Database (PostgreSQL - Cloud mode default)
        AUTOMATION_DB_HOST: Database host (default: localhost)
        AUTOMATION_DB_PORT: Database port (default: 5432)
        AUTOMATION_DB_NAME: Database name (default: automations)
        AUTOMATION_DB_USER: Database user (default: postgres)
        AUTOMATION_DB_PASS: Database password (default: postgres)
        AUTOMATION_DB_SSL_MODE: PostgreSQL SSL mode: prefer, require, or disable
            (default: empty, use driver default)
        AUTOMATION_DB_POOL_SIZE: Connection pool size (default: 10)
        AUTOMATION_DB_MAX_OVERFLOW: Max overflow connections (default: 5)
        AUTOMATION_DB_POOL_RECYCLE: Pool recycle time in seconds (default: 1800)

        # Database URL (alternative to host/port config, supports SQLite for local mode)
        AUTOMATION_DB_URL: Full database URL (e.g., sqlite+aiosqlite:////data/automations.db)

        # GCP Cloud SQL
        AUTOMATION_GCP_DB_INSTANCE: Cloud SQL instance (optional)
        AUTOMATION_GCP_PROJECT: GCP project (optional)
        AUTOMATION_GCP_REGION: GCP region (optional)

        # Local agent-server mode (self-hosted deployments)
        AUTOMATION_AGENT_SERVER_URL: Local agent server URL (e.g., localhost:3000)
        AUTOMATION_AGENT_SERVER_API_KEY: Session API key for local agent server
        AUTOMATION_SANDBOX_AGENT_SERVER_URL: Optional override for the
            AGENT_SERVER_URL exported into the in-sandbox bash chain. Defaults
            to AUTOMATION_AGENT_SERVER_URL when empty. Use this when the
            backend reaches the agent-server at a different URL than the bash
            chain does (e.g. agent-canvas `dev:docker`, where the backend
            runs on the host and the bash chain runs inside the agent-server
            container).
        AUTOMATION_WORKSPACE_BASE: Base workspace directory (local mode default)

        # Background workers
        AUTOMATION_SCHEDULER_INTERVAL_SECONDS: Scheduler poll interval (default: 60)
        AUTOMATION_SCHEDULER_BATCH_SIZE: Scheduler batch size (default: 50)
        AUTOMATION_DISPATCHER_INTERVAL_SECONDS: Dispatcher poll interval (default: 10)
        AUTOMATION_DISPATCHER_BATCH_SIZE: Dispatcher batch size (default: 10)
        AUTOMATION_WATCHDOG_INTERVAL_SECONDS: Watchdog poll interval (default: 60)

        # API pagination
        AUTOMATION_API_DEFAULT_PAGE_SIZE: Default page size (default: 50)
        AUTOMATION_API_MAX_PAGE_SIZE: Max page size (default: 100)

        # Service
        AUTOMATION_HOST: Bind address (default: 0.0.0.0)
        AUTOMATION_SERVER_PORT: Server port (default: 8000)
        AUTOMATION_BASE_URL: Public base URL (optional)
        AUTOMATION_CORS_ORIGINS: Comma-separated CORS origins (optional)

        # Auth
        AUTOMATION_SERVICE_KEY: Service key for SaaS API (required in cloud mode)
        AUTOMATION_WEBHOOK_SECRET: Webhook signature secret (optional)
        AUTOMATION_OPENHANDS_API_BASE_URL: OpenHands API URL (default: https://app.all-hands.dev)

        # Product telemetry (optional)
        AUTOMATION_POSTHOG_API_KEY: PostHog project key. Empty disables capture.
        AUTOMATION_POSTHOG_HOST: PostHog capture host (default: https://us.i.posthog.com)
    """

    # Database (PostgreSQL - Cloud mode)
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "automations"
    db_user: str = "postgres"
    db_pass: str = "postgres"
    db_ssl_mode: str = ""
    db_pool_size: int = 10
    db_max_overflow: int = 5
    db_pool_recycle: int = 1800  # 30 minutes

    # Database URL (alternative config, supports SQLite for local mode)
    # When set, takes precedence over host/port config.
    # Examples:
    #   - sqlite+aiosqlite:////data/automations.db (local SQLite)
    #   - postgresql+asyncpg://user:pass@host/db (PostgreSQL)
    db_url: str = ""

    # GCP Cloud SQL (if set, takes precedence over host/port)
    gcp_db_instance: str | None = None
    gcp_project: str | None = None
    gcp_region: str | None = None

    # Maximum seconds to wait for a connection from the pool.
    # Prevents indefinite hangs when pool is exhausted due to slow operations.
    # If pool exhaustion is frequent, increase pool_size rather than this timeout.
    db_pool_timeout: float = 30

    # Local agent-server mode (self-hosted deployments)
    # When agent_server_url is set, the service operates in "local mode":
    # - Uses a persistent local agent server instead of cloud sandboxes
    # - Skips per-user API key minting (uses agent_server_api_key instead)
    # - Supports SQLite database via db_url
    # - Authenticates using local_api_key instead of OpenHands SaaS API
    agent_server_url: str = ""
    agent_server_api_key: str = ""
    # Optional override for the AGENT_SERVER_URL env var exported into the
    # in-sandbox bash chain by LocalAgentServerBackend.build_env_vars.
    # When empty, defaults to agent_server_url (the URL the backend itself
    # uses). Needed in container-split dev setups (e.g. agent-canvas
    # `dev:docker`) where the host-side backend reaches the agent-server at
    # `localhost:<hostPort>` but the bash chain runs *inside* the
    # agent-server container and needs `127.0.0.1:8000` or
    # `host.docker.internal:<hostPort>` instead.
    sandbox_agent_server_url: str = ""
    workspace_base: str = "/workspace"

    # Local API key for authentication in local mode (self-hosted deployments)
    # When set and is_local_mode is True, requests with this Bearer token
    # are authenticated as a default local user without calling OpenHands API.
    # Generate a secure random key for production self-hosted deployments.
    local_api_key: str = ""

    # OpenHands SaaS API
    openhands_api_base_url: str = "https://app.all-hands.dev"

    # Background workers
    scheduler_interval_seconds: int = 60
    scheduler_batch_size: int = 50
    dispatcher_interval_seconds: int = 10
    dispatcher_batch_size: int = 10
    watchdog_interval_seconds: int = 60

    # How long an accepted event stays in `integration_events`. It bounds two
    # things: the dedupe window (a redelivery older than this is indistinguishable
    # from a new event) and the table, which otherwise grows with every delivery.
    # Well past any provider's own retry horizon -- GitHub gives up after ~3 days.
    integration_event_retention_days: int = 14

    # API pagination
    api_default_page_size: int = 50
    api_max_page_size: int = 100

    # Service key for authenticating with the SaaS API to fetch per-user
    # API keys (called by the dispatcher before each automation run).
    # Required in cloud mode, not needed in local mode.
    service_key: str = ""

    # Public base URL where this service is reachable (without /api/automation).
    # Example: https://app.all-hands.dev or https://domain/acmecorp
    # The /api/automation path is appended automatically by resolved_base_url.
    # If empty, falls back to http://localhost:{server_port} (dev only).
    base_url: str = ""

    # Service
    host: str = "0.0.0.0"
    # Use "server_port" to avoid collision with Kubernetes service discovery
    # (K8s auto-injects AUTOMATION_PORT=tcp://... for the 'automation' service)
    server_port: int = 8000
    log_level: str = "info"

    # CORS origins (comma-separated list, defaults to openhands_api_base_url)
    cors_origins: str = ""

    # Event-based triggers: Shared secret for verifying webhook signatures
    # Used by the OpenHands server when forwarding GitHub events
    webhook_secret: str = ""

    # Optional PostHog product telemetry. Capture remains disabled unless a
    # project key is configured by the deployment.
    posthog_api_key: str = ""
    posthog_host: str = "https://us.i.posthog.com"

    model_config = {"env_prefix": "AUTOMATION_"}

    @model_validator(mode="after")
    def apply_db_ssl_mode_env_fallback(self) -> "ServiceSettings":
        """Match migration env fallback for standard Postgres SSL variables."""
        if "db_ssl_mode" not in self.model_fields_set:
            self.db_ssl_mode = os.getenv("DB_SSL_MODE", os.getenv("PGSSLMODE", ""))
        return self

    @property
    def is_local_mode(self) -> bool:
        """Check if running in local agent-server mode.

        Local mode is enabled when agent_server_url is configured. In this mode:
        - Uses a persistent local agent server instead of cloud sandboxes
        - Skips per-user API key minting
        - No sandbox creation/deletion lifecycle
        """
        return bool(self.agent_server_url)

    @property
    def base_path(self) -> str:
        """Route prefix derived from base_url path component + /api/automation.

        Examples:
            base_url=""                          -> /api/automation
            base_url="https://domain"            -> /api/automation
            base_url="https://domain/acmecorp"   -> /acmecorp/api/automation
        """
        if self.base_url:
            prefix = urlparse(self.base_url).path.rstrip("/")
        else:
            prefix = ""
        return f"{prefix}/api/automation"

    @property
    def resolved_base_url(self) -> str:
        """Public base URL with /api/automation appended."""
        base = self.base_url or f"http://localhost:{self.server_port}"
        return f"{base.rstrip('/')}/api/automation"


# Hardcoded internal URL scheme for uploaded tarballs.
# This is not configurable - changing it would require a database migration
# to update all existing tarball_path references.
INTERNAL_URL_SCHEME = "oh-internal"


# ---------------------------------------------------------------------------
# AppConfig - Composed root configuration
# ---------------------------------------------------------------------------


class AppConfig:
    """Root configuration composing all settings sections.

    This class provides a single entry point for all configuration. Settings
    are loaded lazily on first access and cached using @cached_property.

    Attributes:
        service: Core service settings (database, API, workers)
        storage: File storage backend settings (GCS/S3)
        log: Logging settings
        http: HTTP client settings (timeouts, caching)
        sandbox: Sandbox execution settings (limits, retries)
        kv: Key-value store settings (secrets, limits)
        git_sync: Git sync settings (repo, branch, credentials)
        streams: Stream source settings (Slack Socket Mode)

    Example:
        config = get_config()
        print(config.service.db_host)
        print(config.storage.file_store)
        print(config.log.log_level)
        print(config.sandbox.default_run_duration)
        print(config.kv.enabled)
    """

    @cached_property
    def service(self) -> ServiceSettings:
        """Core service configuration (AUTOMATION_ prefix)."""
        return ServiceSettings()

    @cached_property
    def storage(self) -> StorageSettings:
        """File storage configuration (no prefix)."""
        return StorageSettings()

    @cached_property
    def log(self) -> LogSettings:
        """Logging configuration (no prefix)."""
        return LogSettings()

    @cached_property
    def http(self) -> HttpSettings:
        """HTTP client configuration (AUTOMATION_ prefix)."""
        return HttpSettings()

    @cached_property
    def sandbox(self) -> SandboxSettings:
        """Sandbox execution configuration (AUTOMATION_ prefix)."""
        return SandboxSettings()

    @cached_property
    def kv(self) -> KVSettings:
        """Key-value store configuration (AUTOMATION_ prefix)."""
        return KVSettings()

    @cached_property
    def git_sync(self) -> GitSyncSettings:
        """Git sync configuration (AUTOMATION_ prefix)."""
        return GitSyncSettings()

    @cached_property
    def streams(self) -> StreamSettings:
        """Stream source configuration (AUTOMATION_ prefix)."""
        return StreamSettings()


@lru_cache
def get_config() -> AppConfig:
    """Get the application configuration singleton.

    Returns:
        AppConfig instance with all settings sections.

    Example:
        config = get_config()
        config.service.db_host
        config.storage.file_store
        config.log.log_level
    """
    return AppConfig()


def clear_config_cache() -> None:
    """Clear the config cache. Useful for testing with different env vars.

    This clears the lru_cache for get_config(), forcing settings to be
    reloaded from environment variables on next access. It also resets
    the auth cache so new cache settings (TTL, size) take effect.

    Note:
        This does NOT reset module-level values that were captured at import
        time, such as:
        - Retry decorators in auth.py and execution.py (tenacity config)
        - Logging settings in logger.py (LOG_LEVEL, LOG_JSON, etc.)

        These values are intentionally frozen at import for performance.
        If tests need to modify these behaviors, use monkeypatching or
        reload the affected modules.
    """
    get_config.cache_clear()

    # Reset auth cache so new config values (TTL, size) take effect
    from openhands.automation.auth import _reset_auth_cache

    _reset_auth_cache()


# ---------------------------------------------------------------------------
# Backward-compatible aliases
# ---------------------------------------------------------------------------

# Type alias for backward compatibility
Settings = ServiceSettings

# Track which deprecated functions have already warned to avoid spam
_warned_functions: set[str] = set()


def get_settings() -> ServiceSettings:
    """Get core service settings.

    DEPRECATED: Use get_config().service instead.

    Returns:
        ServiceSettings instance (same as get_config().service).
    """
    if "get_settings" not in _warned_functions:
        warnings.warn(
            "get_settings() is deprecated. Use get_config().service instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        _warned_functions.add("get_settings")
    return get_config().service


def get_storage_settings() -> StorageSettings:
    """Get storage backend settings.

    DEPRECATED: Use get_config().storage instead.

    Returns:
        StorageSettings instance (same as get_config().storage).
    """
    if "get_storage_settings" not in _warned_functions:
        warnings.warn(
            "get_storage_settings() is deprecated. Use get_config().storage instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        _warned_functions.add("get_storage_settings")
    return get_config().storage


def get_log_settings() -> LogSettings:
    """Get logging settings.

    DEPRECATED: Use get_config().log instead.

    Returns:
        LogSettings instance (same as get_config().log).
    """
    if "get_log_settings" not in _warned_functions:
        warnings.warn(
            "get_log_settings() is deprecated. Use get_config().log instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        _warned_functions.add("get_log_settings")
    return get_config().log
