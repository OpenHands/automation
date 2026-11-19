"""Automation outcome parser validation and typed value helpers."""

import json
import math
from collections.abc import Sequence
from datetime import datetime
from statistics import mean
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from openhands.automation.filter_eval import (
    FilterEvaluationError,
    evaluate_expression,
    validate_payload_expression,
)
from openhands.automation.models import AutomationOutcomeValue


MAX_OUTCOME_PARSERS = 50
MAX_OUTCOME_EXPRESSION_LENGTH = 2000
MAX_OUTCOME_KEYS = 50
MAX_OUTCOME_PAYLOAD_BYTES = 64 * 1024
MAX_OUTCOME_STRING_LENGTH = 2048
OUTCOME_KEY_PATTERN = r"^[a-zA-Z0-9_.-]+$"

OutcomeValueType = Literal["number", "string", "boolean", "datetime", "json"]
OutcomeAggregation = Literal[
    "sum", "count", "avg", "min", "max", "count_true", "count_false"
]


class OutcomeParserDefinition(BaseModel):
    """Declarative extraction rule for a submitted automation outcome."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(..., min_length=1, max_length=100, pattern=OUTCOME_KEY_PATTERN)
    expression: str = Field(..., min_length=1, max_length=MAX_OUTCOME_EXPRESSION_LENGTH)
    value_type: OutcomeValueType

    @field_validator("expression")
    @classmethod
    def validate_expression(cls, value: str) -> str:
        valid, error = validate_payload_expression(value)
        if not valid:
            raise ValueError(f"Invalid JMESPath expression: {error}")
        return value


def validate_outcome_parsers(
    parsers: list[OutcomeParserDefinition] | None,
) -> list[OutcomeParserDefinition] | None:
    if parsers is None:
        return None
    if len(parsers) > MAX_OUTCOME_PARSERS:
        raise ValueError(f"At most {MAX_OUTCOME_PARSERS} outcome parsers are allowed")
    keys = [parser.key for parser in parsers]
    if len(keys) != len(set(keys)):
        raise ValueError("outcome parser keys must be unique")
    return parsers


def _json_schema_type(value_type: OutcomeValueType) -> str:
    if value_type == "number":
        return "number"
    if value_type == "boolean":
        return "boolean"
    if value_type == "string" or value_type == "datetime":
        return "string"
    return "object"


def build_outcome_payload_schema(
    parsers: Sequence[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """Describe the payload accepted by the outcome submission endpoint."""
    if not parsers:
        return None

    properties: dict[str, dict[str, Any]] = {}
    required: list[str] = []
    for raw_parser in parsers:
        parser = OutcomeParserDefinition.model_validate(raw_parser)
        property_schema: dict[str, Any] = {
            "type": _json_schema_type(parser.value_type),
            "description": (
                f"Extracted from submitted payload using JMESPath: {parser.expression}"
            ),
        }
        if parser.value_type == "datetime":
            property_schema["format"] = "date-time"
        elif parser.value_type == "json":
            property_schema = {
                "description": property_schema["description"],
            }
        properties[parser.key] = property_schema
        required.append(parser.key)

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["payload"],
        "properties": {
            "payload": {
                "type": "object",
                "description": (
                    "Submit this object to POST /v1/outcomes. The service evaluates "
                    "the configured parser expressions against it and stores the "
                    "normalized outcome keys described below."
                ),
                "x-openhands-outcome-values": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": required,
                    "properties": properties,
                },
            }
        },
    }


def validate_outcome_payload_size(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        encoded = json.dumps(payload, allow_nan=False, ensure_ascii=False).encode()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"payload must be strict JSON: {exc}") from exc
    if len(encoded) > MAX_OUTCOME_PAYLOAD_BYTES:
        raise ValueError(
            f"payload exceeds maximum size of {MAX_OUTCOME_PAYLOAD_BYTES} bytes"
        )
    return payload


def _validate_key(key: str) -> str:
    OutcomeParserDefinition(
        key=key,
        expression="@",
        value_type="json",
    )
    return key


def _validate_string(value: str, key: str) -> str:
    if len(value) > MAX_OUTCOME_STRING_LENGTH:
        raise ValueError(
            f"Outcome value '{key}' exceeds {MAX_OUTCOME_STRING_LENGTH} characters"
        )
    return value


def _validate_number(value: Any, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"Outcome value '{key}' is not a number")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError(f"Outcome value '{key}' must be finite")
    return numeric


def _validate_datetime(value: Any, key: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"Outcome value '{key}' is not an ISO datetime string")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"Outcome value '{key}' is not an ISO datetime string"
        ) from exc


def _validate_json_value(value: Any, key: str) -> Any:
    if value is None:
        raise ValueError(f"Outcome value '{key}' cannot be null")
    try:
        json.dumps(value, allow_nan=False, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Outcome value '{key}' is not strict JSON: {exc}") from exc
    return value


def _typed_columns(
    key: str, value: Any, value_type: OutcomeValueType
) -> dict[str, Any]:
    columns: dict[str, Any] = {
        "key": key,
        "value_type": value_type,
        "numeric_value": None,
        "text_value": None,
        "boolean_value": None,
        "datetime_value": None,
        "json_value": None,
    }
    if value_type == "number":
        columns["numeric_value"] = _validate_number(value, key)
    elif value_type == "string":
        if not isinstance(value, str):
            raise ValueError(f"Outcome value '{key}' is not a string")
        columns["text_value"] = _validate_string(value, key)
    elif value_type == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"Outcome value '{key}' is not a boolean")
        columns["boolean_value"] = value
    elif value_type == "datetime":
        columns["datetime_value"] = _validate_datetime(value, key)
    elif value_type == "json":
        columns["json_value"] = _validate_json_value(value, key)
    else:
        raise ValueError(f"Unsupported outcome value type: {value_type}")
    return columns


def _flat_value_type(value: Any, key: str) -> OutcomeValueType:
    if value is None:
        raise ValueError(f"Outcome value '{key}' cannot be null")
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        _validate_number(value, key)
        return "number"
    if isinstance(value, str):
        _validate_string(value, key)
        return "string"
    raise ValueError(
        f"Outcome value '{key}' must be a string, finite number, or boolean"
    )


def extract_outcome_value_columns(
    payload: dict[str, Any],
    parsers: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Parse a submitted outcome payload into typed value column mappings."""

    validate_outcome_payload_size(payload)
    if not parsers:
        if len(payload) > MAX_OUTCOME_KEYS:
            raise ValueError(f"At most {MAX_OUTCOME_KEYS} outcome keys are allowed")
        return [
            _typed_columns(_validate_key(key), value, _flat_value_type(value, key))
            for key, value in payload.items()
        ]

    if len(parsers) > MAX_OUTCOME_KEYS:
        raise ValueError(f"At most {MAX_OUTCOME_KEYS} parsed outcome keys are allowed")
    columns: list[dict[str, Any]] = []
    for raw_parser in parsers:
        parser = OutcomeParserDefinition.model_validate(raw_parser)
        try:
            value = evaluate_expression(parser.expression, payload)
        except FilterEvaluationError as exc:
            raise ValueError(f"Outcome parser '{parser.key}' failed: {exc}") from exc
        if value is None:
            raise ValueError(
                f"Outcome parser '{parser.key}' returned null or missing value"
            )
        columns.append(_typed_columns(parser.key, value, parser.value_type))
    return columns


def aggregate_outcome_values(
    values: Sequence[AutomationOutcomeValue], operation: OutcomeAggregation
) -> float | int | None:
    if operation == "count":
        return len(values)
    if operation == "count_true":
        return sum(1 for value in values if value.boolean_value is True)
    if operation == "count_false":
        return sum(1 for value in values if value.boolean_value is False)
    numbers = [
        value.numeric_value for value in values if value.numeric_value is not None
    ]
    if not numbers:
        return None
    if operation == "sum":
        return sum(numbers)
    if operation == "avg":
        return mean(numbers)
    if operation == "min":
        return min(numbers)
    if operation == "max":
        return max(numbers)
    raise ValueError(f"Unsupported aggregate operation: {operation}")
