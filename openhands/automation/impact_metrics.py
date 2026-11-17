"""Impact metric definition validation and extraction helpers."""

import json
import logging
import uuid
from collections.abc import Sequence
from datetime import datetime
from statistics import mean
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.filter_eval import (
    FilterEvaluationError,
    evaluate_expression,
    validate_payload_expression,
)
from openhands.automation.models import (
    Automation,
    AutomationRun,
    AutomationRunImpactValue,
)


logger = logging.getLogger(__name__)

MAX_IMPACT_METRICS = 20
MAX_IMPACT_EXPRESSION_LENGTH = 2000
IMPACT_METRIC_KEY_PATTERN = r"^[a-zA-Z0-9_.-]+$"

ImpactValueType = Literal["number", "string", "boolean", "json"]
ImpactAggregation = Literal["sum", "count", "avg", "min", "max"]


class ImpactMetricDefinition(BaseModel):
    """Declarative extraction rule for a per-run impact fact."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(
        ..., min_length=1, max_length=100, pattern=IMPACT_METRIC_KEY_PATTERN
    )
    label: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    expression: str = Field(..., min_length=1, max_length=MAX_IMPACT_EXPRESSION_LENGTH)
    value_type: ImpactValueType
    unit: str | None = Field(default=None, max_length=50)
    default_aggregation: ImpactAggregation = "sum"

    @field_validator("expression")
    @classmethod
    def validate_expression(cls, value: str) -> str:
        valid, error = validate_payload_expression(value)
        if not valid:
            raise ValueError(f"Invalid JMESPath expression: {error}")
        return value


def validate_impact_metrics(
    metrics: list[ImpactMetricDefinition] | None,
) -> list[ImpactMetricDefinition] | None:
    if metrics is None:
        return None
    if len(metrics) > MAX_IMPACT_METRICS:
        raise ValueError(f"At most {MAX_IMPACT_METRICS} impact metrics are allowed")
    keys = [metric.key for metric in metrics]
    if len(keys) != len(set(keys)):
        raise ValueError("impact metric keys must be unique")
    return metrics


def _projection_for_value(
    value: Any, value_type: ImpactValueType
) -> tuple[float | None, str | None, bool | None, str | None]:
    if value_type == "number":
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None, None, None, "extracted value is not a number"
        return float(value), None, None, None
    if value_type == "string":
        if not isinstance(value, str):
            return None, None, None, "extracted value is not a string"
        return None, value, None, None
    if value_type == "boolean":
        if not isinstance(value, bool):
            return None, None, None, "extracted value is not a boolean"
        return None, None, value, None
    return None, None, None, None


def _json_safe_value(value: Any) -> Any:
    json.dumps(value)
    return value


def _fact_values(
    *,
    metric: ImpactMetricDefinition,
    value: Any = None,
    extraction_error: str | None = None,
) -> dict[str, Any]:
    numeric_value = text_value = boolean_value = None
    if extraction_error is None:
        try:
            value = _json_safe_value(value)
        except (TypeError, ValueError) as exc:
            extraction_error = f"extracted value is not JSON serializable: {exc}"
    if extraction_error is None:
        numeric_value, text_value, boolean_value, extraction_error = (
            _projection_for_value(value, metric.value_type)
        )
    return {
        "metric_key": metric.key,
        "metric_label": metric.label,
        "expression": metric.expression,
        "value_type": metric.value_type,
        "value_json": value,
        "numeric_value": numeric_value,
        "text_value": text_value,
        "boolean_value": boolean_value,
        "extraction_error": extraction_error,
    }


def _evaluate_metric(
    metric: ImpactMetricDefinition, finish_response: Any
) -> dict[str, Any]:
    try:
        value = evaluate_expression(metric.expression, finish_response)
        return _fact_values(metric=metric, value=value)
    except FilterEvaluationError as exc:
        return _fact_values(metric=metric, extraction_error=str(exc))


async def store_run_impact_values(
    session: AsyncSession,
    *,
    automation: Automation,
    run: AutomationRun,
    finish_response: Any,
    completed_at: datetime,
) -> None:
    """Extract and upsert configured impact facts for a terminal run."""

    if not automation.impact_metrics:
        return

    for raw_metric in automation.impact_metrics:
        try:
            metric = ImpactMetricDefinition.model_validate(raw_metric)
        except ValueError as exc:
            logger.warning(
                "Skipping invalid impact metric definition for automation %s: %s",
                automation.id,
                exc,
            )
            continue

        values = {
            "id": uuid.uuid4(),
            "org_id": automation.org_id,
            "automation_id": automation.id,
            "run_id": run.id,
            "run_completed_at": completed_at,
            **_evaluate_metric(metric, finish_response),
        }
        result = await session.execute(
            select(AutomationRunImpactValue).where(
                AutomationRunImpactValue.run_id == run.id,
                AutomationRunImpactValue.metric_key == metric.key,
            )
        )
        existing = result.scalars().first()
        if existing is None:
            session.add(AutomationRunImpactValue(**values))
            continue
        values.pop("id")
        for field, value in values.items():
            setattr(existing, field, value)


def aggregate_numeric_values(
    values: Sequence[AutomationRunImpactValue], operation: ImpactAggregation
) -> float | int | None:
    if operation == "count":
        return len(values)
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
