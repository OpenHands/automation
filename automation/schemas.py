"""Pydantic request/response schemas for the API."""

import re
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Literal

from croniter import croniter
from pydantic import BaseModel, Field, field_validator


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
    trigger: CronTrigger
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


class UpdateAutomationRequest(BaseModel):
    """Request to partially update an automation."""

    name: str | None = Field(default=None, min_length=1, max_length=500)
    trigger: CronTrigger | None = None
    tarball_path: str | None = Field(default=None)
    setup_script_path: str | None = Field(default=None)
    entrypoint: str | None = Field(default=None)
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


# --- Responses ---


class AutomationResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    org_id: uuid.UUID
    name: str
    triggers: dict
    tarball_path: str
    setup_script_path: str | None
    entrypoint: str
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
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    model_config = {"from_attributes": True}


class AutomationRunListResponse(BaseModel):
    """Response for listing automation runs (Phase 1b)."""

    runs: list[AutomationRunResponse]
    total: int
