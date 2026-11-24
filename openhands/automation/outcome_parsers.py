"""Optional extraction rules for nested outcome payloads."""

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from openhands.automation.filter_eval import (
    FilterEvaluationError,
    evaluate_expression,
    validate_payload_expression,
)
from openhands.automation.outcome_values import (
    MAX_OUTCOME_KEYS,
    OUTCOME_KEY_PATTERN,
    OutcomeValueType,
    validate_outcome_payload_size,
)


MAX_OUTCOME_EXPRESSION_LENGTH = 2000


class OutcomeParserDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(..., min_length=1, max_length=100, pattern=OUTCOME_KEY_PATTERN)
    expression: str = Field(..., min_length=1, max_length=MAX_OUTCOME_EXPRESSION_LENGTH)
    value_type: OutcomeValueType
    required: bool = Field(default=True, exclude_if=lambda value: value)

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
    if len(parsers) > MAX_OUTCOME_KEYS:
        raise ValueError(f"At most {MAX_OUTCOME_KEYS} outcome parsers are allowed")
    keys = [parser.key for parser in parsers]
    if len(keys) != len(set(keys)):
        raise ValueError("outcome parser keys must be unique")
    return parsers


def parse_outcome_payload(
    payload: dict[str, Any], parsers: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Extract typed facts; the core writer validates them before persistence."""
    validate_outcome_payload_size(payload)
    definitions = (
        validate_outcome_parsers(
            [OutcomeParserDefinition.model_validate(item) for item in parsers]
        )
        or []
    )
    values = []
    for parser in definitions:
        try:
            value = evaluate_expression(parser.expression, payload)
        except FilterEvaluationError as exc:
            raise ValueError(f"Outcome parser '{parser.key}' failed: {exc}") from exc
        if value is None:
            if not parser.required:
                continue
            raise ValueError(
                f"Outcome parser '{parser.key}' returned null or missing value"
            )
        values.append(
            {"key": parser.key, "value_type": parser.value_type, "value": value}
        )
    return values


def build_outcome_payload_schema(
    parsers: Sequence[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    if not parsers:
        return None
    properties: dict[str, dict[str, Any]] = {}
    required: list[str] = []
    for raw in parsers:
        parser = OutcomeParserDefinition.model_validate(raw)
        if parser.required:
            required.append(parser.key)
        property_schema: dict[str, Any] = {
            "type": (
                "string"
                if parser.value_type in ("string", "datetime")
                else "object"
                if parser.value_type == "json"
                else parser.value_type
            ),
            "description": f"Extracted using JMESPath: {parser.expression}",
        }
        if parser.value_type == "datetime":
            property_schema["format"] = "date-time"
        elif parser.value_type == "json":
            property_schema.pop("type")
        properties[parser.key] = property_schema
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
                    "the configured parser expressions and stores the normalized "
                    "outcome keys described below."
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
