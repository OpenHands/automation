"""Schemas for automation outcome submissions."""

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from openhands.automation.outcome_values import validate_outcome_payload_size


class SubmitOutcomeRequest(BaseModel):
    """Outcome payload submitted by an automation run or authenticated user."""

    model_config = ConfigDict(extra="forbid")

    payload: dict[str, Any] = Field(..., min_length=1)
    run_id: uuid.UUID | None = None

    @field_validator("payload")
    @classmethod
    def validate_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        return validate_outcome_payload_size(value)


class SubmitOutcomeResponse(BaseModel):
    stored: int
