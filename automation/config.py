"""Application configuration loaded from environment variables."""

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

    # Use SQLite for local dev (set to empty string to disable)
    sqlite_path: str | None = None

    # Pool settings
    db_pool_size: int = 10
    db_max_overflow: int = 5

    # OpenHands SaaS API
    openhands_api_base_url: str = "https://app.openhands.ai"

    # Scheduler
    scheduler_interval_seconds: int = 60

    # Encryption key for stored API keys (Fernet-compatible base64 key)
    encryption_key: str = ""

    # Service
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "info"

    model_config = {"env_prefix": "AUTOMATION_"}


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
