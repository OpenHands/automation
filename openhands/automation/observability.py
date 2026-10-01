"""Laminar/OpenTelemetry helpers for the automation service.

This module keeps tracing optional: if Laminar/OTEL environment variables are
not configured, every helper is a no-op and service behavior is unchanged.
"""

from __future__ import annotations

import contextlib
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
def span(name: str, attributes: Mapping[str, Any] | None = None) -> Iterator[Any]:
    """Start a Laminar/OTEL span if tracing is configured, else no-op."""
    if not observability_enabled():
        yield None
        return
    try:
        from lmnr import Laminar

        span_context = Laminar.start_as_current_span(name=name)
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


def inject_trace_context(carrier: MutableMapping[str, str]) -> None:
    """Inject the active OTEL trace context into a mutable carrier."""
    if not observability_enabled():
        return
    try:
        from opentelemetry.propagate import inject

        inject(carrier)
    except Exception:
        logger.debug("Failed to inject trace context", exc_info=True)


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
        attributes.update(
            {
                "automation.run_id": str(run.id),
                "automation.run.status": run.status.value if run.status else None,
                "automation.run.trigger_source": run.trigger_source,
                "automation.conversation_id": run.conversation_id,
                "openhands.conversation_id": run.conversation_id,
                "automation.sandbox_id": run.sandbox_id,
                "automation.bash_command_id": run.bash_command_id,
            }
        )
        if automation is None:
            attributes["automation.id"] = str(run.automation_id)
    attributes.update(extra)
    return {key: value for key, value in attributes.items() if value is not None}


def automation_env_metadata(
    automation: Automation, run: AutomationRun
) -> dict[str, str]:
    """Environment variables exposing authoritative automation correlation IDs."""
    trigger = automation.trigger if isinstance(automation.trigger, dict) else {}
    trigger_source = run.trigger_source or str(trigger.get("type") or "")
    return {
        "AUTOMATION_ID": str(automation.id),
        "AUTOMATION_NAME": automation.name,
        "AUTOMATION_RUN_ID": str(run.id),
        "AUTOMATION_ORG_ID": str(automation.org_id),
        "AUTOMATION_USER_ID": str(automation.user_id),
        "AUTOMATION_TRIGGER_SOURCE": trigger_source,
    }
