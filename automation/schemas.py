"""Pydantic request/response schemas for the API."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class CronTrigger(BaseModel):
    schedule: str = Field(..., description="Cron expression, e.g. '0 9 * * 5'")
    timezone: str = Field(default="UTC", description="IANA timezone name")


class TriggerConfig(BaseModel):
    cron: CronTrigger | None = None


# --- Requests ---


class CreateAutomationRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=500)
    triggers: TriggerConfig
    sdk_code_tarball_path: str = Field(
        ..., description="S3/GCS path to the SDK code tarball"
    )
    api_key: str = Field(
        ..., description="OpenHands API key for V1 API auth (stored encrypted)"
    )


class UpdateAutomationRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=500)
    triggers: TriggerConfig | None = None
    sdk_code_tarball_path: str | None = None
    api_key: str | None = Field(
        default=None, description="New API key (re-encrypted on update)"
    )
    enabled: bool | None = None


# --- Responses ---


class AutomationResponse(BaseModel):
    id: uuid.UUID
    user_id: str
    name: str
    triggers: dict
    sdk_code_tarball_path: str
    enabled: bool
    last_triggered_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AutomationRunResponse(BaseModel):
    id: uuid.UUID
    automation_id: uuid.UUID
    status: str
    conversation_id: str | None
    trigger_type: str
    error_detail: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class AutomationListResponse(BaseModel):
    automations: list[AutomationResponse]
    total: int


class RunListResponse(BaseModel):
    runs: list[AutomationRunResponse]
    total: int
