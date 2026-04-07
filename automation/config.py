"""Application configuration loaded from environment variables."""

from functools import lru_cache
from urllib.parse import urlparse

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "automations"
    db_user: str = "postgres"
    db_pass: str = "postgres"

    # GCP Cloud SQL (if set, takes precedence over host/port)
    gcp_db_instance: str | None = None
    gcp_project: str | None = None
    gcp_region: str | None = None

    # Pool settings
    db_pool_size: int = 10
    db_max_overflow: int = 5

    # OpenHands SaaS API
    openhands_api_base_url: str = "https://app.all-hands.dev"

    # Scheduler (polls automations table for due cron jobs)
    scheduler_interval_seconds: int = 60

    # Dispatcher (polls automation_runs table for pending jobs)
    dispatcher_interval_seconds: int = 10

    # Watchdog (scans for stale RUNNING runs past their timeout)
    watchdog_interval_seconds: int = 60

    # Service key for authenticating with the SaaS API to fetch per-user
    # API keys (called by the dispatcher before each automation run).
    service_key: str = ""

    # Public URL for the automation service (used for sandbox callbacks).
    # In production, this is the ingress URL (e.g., https://automation.all-hands.dev).
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

    model_config = {"env_prefix": "AUTOMATION_"}

    @property
    def resolved_base_url(self) -> str:
        """Public base URL with localhost fallback for dev."""
        return self.base_url or f"http://localhost:{self.server_port}"

    @property
    def root_path(self) -> str:
        """Root path for Swagger UI, derived from base_url.

        When base_url is set (e.g., https://app.all-hands.dev/api/automation),
        extracts the path component (/api/automation) for FastAPI's root_path.
        This allows Swagger UI to correctly resolve the OpenAPI spec URL.
        """
        if self.base_url:
            return urlparse(self.base_url).path.rstrip("/")
        return ""


# Hardcoded internal URL scheme for uploaded tarballs.
# This is not configurable - changing it would require a database migration
# to update all existing tarball_path references.
INTERNAL_URL_SCHEME = "oh-internal"


@lru_cache
def get_settings() -> Settings:
    return Settings()
