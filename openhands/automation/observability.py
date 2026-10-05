"""Laminar/OpenTelemetry helpers for the automation service.

This module keeps tracing optional: if Laminar/OTEL environment variables are
not configured, every helper is a no-op and service behavior is unchanged.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Iterator, Mapping, MutableMapping
from typing import Any

from openhands.automation.models import Automation, AutomationRun


logger = logging.getLogger("automation.observability")

TraceValue = str | bool | int | float | list[str] | list[bool] | list[int] | list[float]


def _trace_value(value: Any) -> TraceValue | None:
    """Return an OpenTelemetry-compatible attribute value, or None."""
    if value is None:
        return None
    if isinstance(value, str | bool | int | float):
        return value
    if isinstance(value, list) and value:
        if all(isinstance(item, str) for item in value):
            return value
        if all(isinstance(item, bool) for item in value):
            return value
        if all(isinstance(item, int) and not isinstance(item, bool) for item in value):
            return value
        if all(isinstance(item, float) for item in value):
            return value
    return str(value)


def _clean_attributes(attributes: Mapping[str, Any] | None) -> dict[str, TraceValue]:
    cleaned: dict[str, TraceValue] = {}
    for key, value in (attributes or {}).items():
        if not key:
            continue
        trace_value = _trace_value(value)
        if trace_value is not None:
            cleaned[key] = trace_value
    return cleaned


def init_observability() -> None:
    """Initialize SDK Laminar/OTEL integration when configured."""
    try:
        from openhands.sdk.observability import maybe_init_laminar

        maybe_init_laminar()
    except Exception:
        logger.debug("Failed to initialize automation observability", exc_info=True)


def observability_enabled() -> bool:
    """Return whether SDK observability is enabled."""
    try:
        from openhands.sdk.observability.laminar import should_enable_observability

        return should_enable_observability()
    except Exception:
        return False


@contextlib.contextmanager
def span(
    name: str,
    attributes: Mapping[str, Any] | None = None,
    parent_span_context: str | None = None,
) -> Iterator[Any]:
    """Start a Laminar/OTEL span if tracing is configured, else no-op."""
    if not observability_enabled():
        yield None
        return
    try:
        from lmnr import Laminar

        parent = None
        if parent_span_context:
            with contextlib.suppress(Exception):
                parent = Laminar.deserialize_span_context(parent_span_context)
        span_context = Laminar.start_as_current_span(
            name=name,
            parent_span_context=parent,
        )
    except Exception:
        logger.debug("Failed to create observability span %s", name, exc_info=True)
        yield None
        return

    with span_context as current_span:
        for key, value in _clean_attributes(attributes).items():
            with contextlib.suppress(Exception):
                current_span.set_attribute(key, value)
        yield current_span


def add_event(name: str, attributes: Mapping[str, Any] | None = None) -> None:
    """Attach an event to the current OTEL span when available."""
    if not observability_enabled():
        return
    try:
        from opentelemetry import trace

        current_span = trace.get_current_span()
        if current_span is not None and current_span.is_recording():
            current_span.add_event(name, _clean_attributes(attributes))
    except Exception:
        logger.debug("Failed to add observability event %s", name, exc_info=True)


def current_span_context() -> str | None:
    """Return the current serialized Laminar span context, when available."""
    if not observability_enabled():
        return None
    try:
        from lmnr import Laminar

        return Laminar.serialize_span_context() or None
    except Exception:
        logger.debug("Failed to serialize Laminar span context", exc_info=True)
        return None


def inject_trace_context(carrier: MutableMapping[str, str]) -> None:
    """Inject active trace context into a mutable environment carrier."""
    if not observability_enabled():
        return

    span_context = current_span_context()
    if span_context:
        carrier["LMNR_SPAN_CONTEXT"] = span_context

    try:
        from opentelemetry.propagate import inject

        inject(carrier)
    except Exception:
        logger.debug("Failed to inject W3C trace context", exc_info=True)


def automation_attributes(
    automation: Automation | None = None,
    run: AutomationRun | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build standard automation trace attributes."""
    attributes: dict[str, Any] = {}
    if automation is not None:
        attributes.update(
            {
                "automation.id": str(automation.id),
                "automation.name": automation.name,
                "automation.org_id": str(automation.org_id),
                "automation.user_id": str(automation.user_id),
            }
        )
        trigger = automation.trigger if isinstance(automation.trigger, dict) else None
        if trigger:
            attributes["automation.trigger_source"] = trigger.get("type")
    if run is not None:
        run_status = getattr(run, "status", None)
        conversation_id = getattr(run, "conversation_id", None)
        trigger_event_id = getattr(run, "trigger_event_id", None)
        attributes.update(
            {
                "automation.run_id": str(run.id),
                "automation.run.status": run_status.value if run_status else None,
                "automation.run.trigger_source": getattr(run, "trigger_source", None),
                "automation.trigger_event_id": str(trigger_event_id)
                if trigger_event_id
                else None,
                "automation.conversation_id": conversation_id,
                "openhands.conversation_id": conversation_id,
                "automation.sandbox_id": getattr(run, "sandbox_id", None),
                "automation.bash_command_id": getattr(run, "bash_command_id", None),
            }
        )
        if automation is None:
            attributes["automation.id"] = str(run.automation_id)
    attributes.update(extra)
    return {key: value for key, value in attributes.items() if value is not None}


def automation_observability_tags(
    automation: Automation, run: AutomationRun
) -> list[str]:
    """Return low-cardinality Laminar tags for an automation run."""
    trigger = automation.trigger if isinstance(automation.trigger, dict) else {}
    trigger_type = str(trigger.get("type") or "")
    run_trigger_source = run.trigger_source or trigger_type
    tags = ["automation"]
    if trigger_type:
        tags.append(f"automation.trigger:{trigger_type}")
    if run_trigger_source:
        tags.append(f"automation.run_trigger:{run_trigger_source}")
    return tags


def automation_env_metadata(
    automation: Automation, run: AutomationRun
) -> dict[str, str]:
    """Environment variables exposing authoritative automation correlation IDs."""
    trigger = automation.trigger if isinstance(automation.trigger, dict) else {}
    trigger_type = str(trigger.get("type") or "")
    run_trigger_source = run.trigger_source or trigger_type
    associations = getattr(run, "observability_associations", None) or {}
    metadata = {**associations, **automation_attributes(automation, run)}
    return {
        "AUTOMATION_ID": str(automation.id),
        "AUTOMATION_NAME": automation.name,
        "AUTOMATION_RUN_ID": str(run.id),
        "AUTOMATION_ORG_ID": str(automation.org_id),
        "AUTOMATION_USER_ID": str(automation.user_id),
        "AUTOMATION_TRIGGER_SOURCE": run_trigger_source,
        "AUTOMATION_TRIGGER_TYPE": trigger_type,
        "AUTOMATION_RUN_TRIGGER_SOURCE": run_trigger_source,
        "OPENHANDS_OBSERVABILITY_METADATA": json.dumps(metadata, separators=(",", ":")),
        "OPENHANDS_OBSERVABILITY_TAGS": ",".join(
            automation_observability_tags(automation, run)
        ),
        "OPENHANDS_OBSERVABILITY_SPAN_NAME": "automation.conversation",
    }
