"""Pydantic request/response schemas for the API."""

import re
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from croniter import croniter
from pydantic import BaseModel, Discriminator, Field, Tag, field_validator

from automation.constants import MAX_RUN_DURATION_SECONDS


# Allowed URI schemes for tarball_path (includes internal upload scheme)
_TARBALL_SCHEME_RE = re.compile(r"^(s3|gs|https?|oh-internal)://")

# Shell metacharacters that should not appear in entrypoints or script paths
_SHELL_META_RE = re.compile(r"[;&|`$(){}<>!\\\n\r]")

# Path traversal pattern
_PATH_TRAVERSAL_RE = re.compile(r"(^|/)\.\.(/|$)")


class CronTrigger(BaseModel):
    """Cron-based trigger configuration."""

    type: Literal["cron"] = "cron"
    schedule: str = Field(..., description="Cron expression, e.g. '0 9 * * 5'")
    timezone: str = Field(default="UTC", description="IANA timezone name")

    @field_validator("schedule")
    @classmethod
    def validate_cron_schedule(cls, v: str) -> str:
        if not croniter.is_valid(v):
            raise ValueError(f"Invalid cron expression: {v}")
        return v


class EventTrigger(BaseModel):
    """
    Event-based trigger configuration.

    Triggers automation when a matching event is received from the source.
    Uses pattern matching via the `on` field and optional JMESPath filter.

    ## Event Key Format

    Events are identified by "{event_type}.{action}" or just "{event_type}" for
    events without actions (like push).

    Examples:
    - `pull_request.opened` - PR opened
    - `pull_request.closed` - PR closed
    - `pull_request.*` - Any PR activity (wildcard)
    - `push` - Code pushed
    - `issue.created` - Linear issue created

    ## Filter Expressions (JMESPath DSL)

    The `filter` field accepts a JMESPath expression that is evaluated against
    the raw webhook payload. The expression must evaluate to a truthy value
    for the event to match.

    **Available functions:**
    - `contains(array, value)` - Check if array contains value
    - `glob(str, pattern)` - Wildcard matching (e.g., 'org/*')
    - `icontains(str, substr)` - Case-insensitive substring match
    - `regex(str, pattern)` - Regular expression match
    - `starts_with(str, prefix)` - Check if string starts with prefix
    - `ends_with(str, suffix)` - Check if string ends with suffix
    - `lower(str)` / `upper(str)` - Case conversion

    **Boolean operators:** `&&` (and), `||` (or), `!` (not)

    ## Examples

    ```json
    // GitHub: Match @openhands-resolver mentions in comments
    {
      "source": "github",
      "on": "issue_comment.created",
      "filter": "icontains(comment.body, '@openhands-resolver')"
    }

    // GitHub: PR opened in specific repo
    {
      "source": "github",
      "on": "pull_request.opened",
      "filter": "repository.full_name == 'myorg/myrepo'"
    }

    // GitHub: PR with 'bug' label in any org repo
    {
      "source": "github",
      "on": "pull_request.opened",
      "filter": "glob(repository.full_name, 'myorg/*')"
    }

    // GitHub: Push to main or release branches
    {
      "source": "github",
      "on": "push",
      "filter": "glob(ref, 'refs/heads/main') || glob(ref, 'refs/heads/release/*')"
    }

    // No filter - match any event of this type
    {"source": "github", "on": "push"}
    ```
    """

    type: Literal["event"] = "event"
    source: str = Field(
        ...,
        description=(
            "Event source: 'github', 'gitlab', 'linear', or custom webhook source name"
        ),
    )
    on: str | list[str] = Field(
        ...,
        description=(
            "Event key pattern(s) to match. "
            "Format: 'event_type.action' or 'event_type'. "
            "Supports wildcards: 'pull_request.*' matches any PR action. "
            "Can be a single pattern or list of patterns."
        ),
    )
    filter: str | None = Field(
        default=None,
        description=(
            "JMESPath expression evaluated against the raw payload. "
            "Must evaluate to truthy for the event to match. "
            "Functions: contains(), glob(), icontains(), regex(). "
            "Example: glob(repository.full_name, 'org/*') && "
            "icontains(comment.body, '@openhands-resolver')"
        ),
    )

    @property
    def event_patterns(self) -> list[str]:
        """Get the event patterns as a list."""
        if isinstance(self.on, str):
            return [self.on]
        return self.on


def _get_trigger_discriminator(v: dict | BaseModel) -> str:
    """Discriminator function for trigger types.

    Returns the trigger type, or a sentinel value if missing.
    Pydantic will generate a validation error for unmatched tags.
    """
    if isinstance(v, dict):
        trigger_type = v.get("type")
        if not trigger_type:
            # Return sentinel that won't match any tag
            # Pydantic generates validation error for unknown tags
            return "__missing_trigger_type__"
        return trigger_type
    return getattr(v, "type")


# Union type for all triggers, using discriminated union
Trigger = Annotated[
    Annotated[CronTrigger, Tag("cron")] | Annotated[EventTrigger, Tag("event")],
    Discriminator(_get_trigger_discriminator),
]


class RunStatus(StrEnum):
    """Status of an automation run (for API responses)."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


def _validate_command_string(
    v: str | None, field_name: str, *, allow_none: bool = True
) -> str | None:
    """Validate a command/path is relative and safe.

    Rejects traversal patterns and shell metacharacters.

    Used for both entrypoint and setup_script_path validation.

    Args:
        v: The value to validate
        field_name: Field name for error messages
        allow_none: If True, None values pass through unchanged

    Returns:
        The validated value
    """
    if v is None:
        if allow_none:
            return v
        raise ValueError(f"{field_name} is required")
    if not v.strip():
        raise ValueError(f"{field_name} must not be blank")
    if v.startswith("/"):
        raise ValueError(f"{field_name} must be a relative path, not an absolute path")
    if _PATH_TRAVERSAL_RE.search(v):
        raise ValueError(f"{field_name} must not contain path traversal (..)")
    if _SHELL_META_RE.search(v):
        raise ValueError(
            f"{field_name} must not contain shell metacharacters (;&|`$(){{}}<>!\\\\)"
        )
    return v


# --- Requests ---


class CreateAutomationRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=500)
    trigger: Trigger = Field(
        ..., description="Trigger configuration (cron or event-based)"
    )
    tarball_path: str = Field(
        ..., description="Path to SDK code tarball (e.g., S3 or GCS URL)"
    )
    setup_script_path: str | None = Field(
        default=None,
        description="Relative path inside tarball to setup script (e.g., setup.sh)",
    )
    entrypoint: str = Field(
        ..., description='Command to execute the automation (e.g., "uv run script.py")'
    )
    timeout: int | None = Field(
        default=None,
        description="Maximum execution time in seconds (default: system maximum)",
    )

    @field_validator("tarball_path")
    @classmethod
    def validate_tarball_path(cls, v: str) -> str:
        if not _TARBALL_SCHEME_RE.match(v):
            raise ValueError(
                "tarball_path must start with s3://, gs://, http://, or https://"
            )
        return v

    @field_validator("setup_script_path")
    @classmethod
    def validate_setup_script_path(cls, v: str | None) -> str | None:
        return _validate_command_string(v, "setup_script_path")

    @field_validator("entrypoint")
    @classmethod
    def validate_entrypoint(cls, v: str) -> str:
        result = _validate_command_string(v, "entrypoint", allow_none=False)
        assert result is not None  # satisfy type checker
        return result

    @field_validator("timeout")
    @classmethod
    def validate_timeout(cls, v: int | None) -> int | None:
        if v is None:
            return v
        if v <= 0:
            raise ValueError("timeout must be a positive number")
        if v > MAX_RUN_DURATION_SECONDS:
            raise ValueError(
                f"timeout must not exceed {MAX_RUN_DURATION_SECONDS} seconds"
            )
        return v


class UpdateAutomationRequest(BaseModel):
    """Request to partially update an automation."""

    name: str | None = Field(default=None, min_length=1, max_length=500)
    trigger: Trigger | None = Field(
        default=None, description="Trigger configuration (cron or event-based)"
    )
    tarball_path: str | None = Field(default=None)
    setup_script_path: str | None = Field(default=None)
    entrypoint: str | None = Field(default=None)
    timeout: int | None = Field(default=None)
    enabled: bool | None = None

    @field_validator("tarball_path")
    @classmethod
    def validate_tarball_path(cls, v: str | None) -> str | None:
        if v is not None and not _TARBALL_SCHEME_RE.match(v):
            raise ValueError(
                "tarball_path must start with s3://, gs://, http://, or https://"
            )
        return v

    @field_validator("setup_script_path")
    @classmethod
    def validate_setup_script_path(cls, v: str | None) -> str | None:
        return _validate_command_string(v, "setup_script_path")

    @field_validator("entrypoint")
    @classmethod
    def validate_entrypoint(cls, v: str | None) -> str | None:
        return _validate_command_string(v, "entrypoint")

    @field_validator("timeout")
    @classmethod
    def validate_timeout(cls, v: int | None) -> int | None:
        if v is None:
            return v
        if v <= 0:
            raise ValueError("timeout must be a positive number")
        if v > MAX_RUN_DURATION_SECONDS:
            raise ValueError(
                f"timeout must not exceed {MAX_RUN_DURATION_SECONDS} seconds"
            )
        return v


# --- Webhook Schemas ---


class WebhookConfig(BaseModel):
    """Configuration for processing a webhook."""

    secret: str
    is_builtin: bool = False  # True for github, gitlab
    event_key_expr: str = "type"  # JMESPath expression for extracting event key

    model_config = {"extra": "forbid"}


class EventResponse(BaseModel):
    """Response for event processing."""

    received: bool
    matched: int
    runs_created: list[str]  # List of run IDs created


# --- Responses ---


class AutomationResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    org_id: uuid.UUID
    name: str
    trigger: dict
    tarball_path: str
    setup_script_path: str | None
    entrypoint: str
    timeout: int | None
    enabled: bool
    last_triggered_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AutomationListResponse(BaseModel):
    automations: list[AutomationResponse]
    total: int


# --- Run schemas ---


class RunCompleteRequest(BaseModel):
    """Payload sent by the SDK's OpenHandsCloudWorkspace on context manager exit."""

    status: Literal["COMPLETED", "FAILED"]
    run_id: str | None = None
    conversation_id: str | None = None
    error: str | None = None


class AutomationRunResponse(BaseModel):
    """Response for a single automation run."""

    id: uuid.UUID
    automation_id: uuid.UUID
    status: RunStatus
    error_detail: str | None
    conversation_id: str | None
    timeout_at: datetime | None
    keep_alive: bool
    sandbox_id: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    model_config = {"from_attributes": True}


class AutomationRunListResponse(BaseModel):
    """Response for listing automation runs (Phase 1b)."""

    runs: list[AutomationRunResponse]
    total: int
