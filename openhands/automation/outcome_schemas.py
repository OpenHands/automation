"""Schemas for automation outcome submissions."""

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from openhands.automation.outcome_values import (
    MAX_OUTCOME_DEDUPE_KEY_LENGTH,
    OutcomeAggregation,
    OutcomeValueType,
    normalize_outcome_dimensions,
    validate_outcome_payload_size,
)
from openhands.automation.utils.time import UtcDatetime


class TypedOutcomeValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(..., min_length=1, max_length=100)
    value_type: OutcomeValueType = Field(alias="type")
    value: Any


class SubmitOutcomeRequest(BaseModel):
    """Exactly one of a flat payload or pre-typed facts."""

    model_config = ConfigDict(extra="forbid")

    payload: dict[str, Any] | None = None
    values: list[TypedOutcomeValue] | None = None
    run_id: uuid.UUID | None = None
    # These four apply to every fact in the submission. Each key accepts the
    # ones that fit its semantics.
    period: str | None = Field(
        default=None,
        description=(
            "The period a total covers: 2026-11-24T14 (hour), 2026-11-24 (day), "
            "2026-W48 (ISO week) or 2026-11 (month), in UTC. The format must "
            "match the grain of the key."
        ),
    )
    effective_at: UtcDatetime | None = Field(
        default=None,
        description=(
            "The time an increment counts at, usually when it happened. "
            "Defaults to now. A total takes its time from period."
        ),
    )
    dedupe_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_OUTCOME_DEDUPE_KEY_LENGTH,
        description=(
            "Names the event an increment reports. A second submission of the "
            "same key and dedupe_key is ignored."
        ),
    )
    dimensions: dict[str, str] | None = Field(
        default=None,
        description=(
            "Labels such as the repository. A later total replaces an earlier "
            "one only when its automation, period and dimensions all match."
        ),
    )

    @model_validator(mode="after")
    def validate_shape(self) -> "SubmitOutcomeRequest":
        if (self.payload is None) == (self.values is None):
            raise ValueError("Provide exactly one of payload or values")
        if self.dimensions is not None:
            self.dimensions = normalize_outcome_dimensions(self.dimensions) or None
        if self.payload is not None:
            if not self.payload:
                raise ValueError("payload must not be empty")
            validate_outcome_payload_size(self.payload)
        return self


class SubmitOutcomeResponse(BaseModel):
    stored: int
    duplicates: int = Field(
        default=0, description="Facts ignored because their dedupe_key was seen."
    )


class OutcomeValueResponse(BaseModel):
    id: uuid.UUID
    factory_id: uuid.UUID
    automation_id: uuid.UUID | None = None
    run_id: uuid.UUID | None = None
    source: str
    key: str
    value_type: str
    numeric_value: float | None = None
    text_value: str | None = None
    boolean_value: bool | None = None
    datetime_value: UtcDatetime | None = None
    json_value: Any | None = None
    created_at: UtcDatetime
    effective_at: UtcDatetime
    dedupe_key: str | None = None
    dimensions: dict[str, str] | None = None

    @computed_field
    @property
    def value(self) -> Any:
        if self.value_type == "number":
            return self.numeric_value
        if self.value_type == "string":
            return self.text_value
        if self.value_type == "boolean":
            return self.boolean_value
        if self.value_type == "datetime":
            return self.datetime_value
        if self.value_type == "json":
            return self.json_value
        return None

    model_config = ConfigDict(from_attributes=True)


class OutcomeValuesResponse(BaseModel):
    values: list[OutcomeValueResponse]
    total: int


class OutcomeAggregateBucket(BaseModel):
    bucket: str
    value: float | int | None
    count: int


class OutcomeAggregateResponse(BaseModel):
    key: str
    operation: OutcomeAggregation
    value: float | int | None = None
    count: int | None = None
    since: UtcDatetime | None = None
    until: UtcDatetime | None = None
    group_by: str | None = None
    buckets: list[OutcomeAggregateBucket] | None = None
