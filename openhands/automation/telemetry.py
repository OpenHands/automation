"""Best-effort PostHog product telemetry for automation lifecycle events."""

import logging
import re
import uuid
from typing import Any

import httpx
from fastapi import Request
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from openhands.automation.auth import AuthenticatedUser
from openhands.automation.config import get_config
from openhands.automation.middleware import (
    TelemetryRequestContext,
    build_telemetry_request_context,
)
from openhands.automation.models import (
    Automation,
    AutomationRun,
    AutomationServiceMetadata,
)


logger = logging.getLogger("automation.telemetry")
AUTOMATION_BACKEND_ID_PROPERTY = "automation_backend_id"
FRONTEND_DISTINCT_ID_PROPERTY = "frontend_distinct_id"
POSTHOG_CAPTURE_PATH = "/capture/"
API_EVENT_PREFIX = "automation_api"
TELEMETRY_BACKEND_DISTINCT_ID_KEY = "posthog_backend_distinct_id"
_PUBLIC_API_ROUTE_PATHS = frozenset(
    {"/health", "/ready", "/sdk-version", "/server_info"}
)


async def _get_or_create_backend_distinct_id(session: AsyncSession) -> str:
    existing = await session.scalar(
        select(AutomationServiceMetadata.value).where(
            AutomationServiceMetadata.key == TELEMETRY_BACKEND_DISTINCT_ID_KEY
        )
    )
    if existing:
        return existing

    generated = f"automation-backend:{uuid.uuid4()}"
    await session.execute(
        text(
            "INSERT INTO automation_service_metadata (key, value) "
            "VALUES (:key, :value) ON CONFLICT (key) DO NOTHING"
        ),
        {"key": TELEMETRY_BACKEND_DISTINCT_ID_KEY, "value": generated},
    )
    return (
        await session.scalar(
            select(AutomationServiceMetadata.value).where(
                AutomationServiceMetadata.key == TELEMETRY_BACKEND_DISTINCT_ID_KEY
            )
        )
        or generated
    )


async def get_automation_backend_distinct_id(
    *,
    request: Request | None = None,
    session: AsyncSession | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> str | None:
    """Return the DB-backed automation backend PostHog distinct ID."""
    try:
        if session is not None:
            return await _get_or_create_backend_distinct_id(session)

        if session_factory is None and request is not None:
            session_factory = getattr(request.app.state, "session_factory", None)

        if session_factory is None:
            logger.debug("No database session available for automation telemetry ID")
            return None

        async with session_factory() as new_session:
            distinct_id = await _get_or_create_backend_distinct_id(new_session)
            await new_session.commit()
            return distinct_id
    except Exception:
        logger.debug("Failed to load automation telemetry backend ID", exc_info=True)
        return None


def get_request_telemetry_context(request: Request | None) -> TelemetryRequestContext:
    if request is None:
        return TelemetryRequestContext()
    context = getattr(request.state, "telemetry_context", None)
    if isinstance(context, TelemetryRequestContext):
        return context
    return build_telemetry_request_context(request.scope)


def _clean_event_suffix(value: str | None) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_]+", "_", value or "unknown").strip("_")
    return cleaned.lower() or "unknown"


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    route_path = getattr(route, "path", None)
    if isinstance(route_path, str) and route_path:
        return route_path
    return request.url.path


def _route_operation(request: Request) -> str:
    endpoint = request.scope.get("endpoint")
    endpoint_name = getattr(endpoint, "__name__", None)
    if isinstance(endpoint_name, str) and endpoint_name:
        return endpoint_name
    route = request.scope.get("route")
    route_name = getattr(route, "name", None)
    return route_name if isinstance(route_name, str) else "unknown"


def should_capture_api_route(request: Request) -> bool:
    path = request.url.path
    settings = get_config().service
    base_path = settings.base_path.rstrip("/")
    base_public_paths = {f"{base_path}{route}" for route in _PUBLIC_API_ROUTE_PATHS}

    return (
        path.startswith(f"{base_path}/v1")
        or path in _PUBLIC_API_ROUTE_PATHS
        or path in base_public_paths
    )


async def capture_api_route_event(
    request: Request,
    *,
    status_code: int,
    duration_ms: int,
    exception_type: str | None = None,
) -> None:
    operation = _clean_event_suffix(_route_operation(request))
    await capture_automation_event(
        f"{API_EVENT_PREFIX}_{operation}",
        request=request,
        properties={
            "http_method": request.method,
            "route_path": _route_template(request),
            "route_operation": operation,
            "status_code": status_code,
            "success": status_code < 400,
            "duration_ms": duration_ms,
            **({"exception_type": exception_type} if exception_type else {}),
        },
    )


def _trigger_type(automation: Automation | None) -> str | None:
    trigger = automation.trigger if automation is not None else None
    if isinstance(trigger, dict):
        value = trigger.get("type")
        return str(value) if value is not None else None
    return str(trigger) if trigger is not None else None


def _resolve_distinct_id(*, backend_distinct_id: str) -> str:
    return backend_distinct_id


def _base_properties(
    *,
    request_context: TelemetryRequestContext,
    user: AuthenticatedUser | None,
    automation: Automation | None,
    run: AutomationRun | None,
    backend_distinct_id: str,
) -> dict[str, Any]:
    settings = get_config().service
    properties: dict[str, Any] = {
        "deployment_mode": "local" if settings.is_local_mode else "cloud",
        "automation_service": "openhands_automation",
    }

    properties[AUTOMATION_BACKEND_ID_PROPERTY] = backend_distinct_id

    if request_context.frontend_distinct_id:
        properties[FRONTEND_DISTINCT_ID_PROPERTY] = request_context.frontend_distinct_id
    if request_context.client_source:
        properties["client_source"] = request_context.client_source
    if request_context.client_version:
        properties["client_version"] = request_context.client_version

    if automation is not None:
        properties.update(
            {
                "automation_id": str(automation.id),
                "automation_enabled": automation.enabled,
                "trigger_type": _trigger_type(automation),
                "timeout_seconds": automation.timeout,
            }
        )
        if not settings.is_local_mode:
            properties.update(
                {
                    "cloud_user_id": str(automation.user_id),
                    "cloud_org_id": str(automation.org_id),
                    "org_id": str(automation.org_id),
                    "$groups": {"org": str(automation.org_id)},
                }
            )

    if user is not None and not settings.is_local_mode:
        properties.update(
            {
                "cloud_user_id": str(user.user_id),
                "cloud_org_id": str(user.org_id),
                "org_id": str(user.org_id),
                "$groups": {"org": str(user.org_id)},
            }
        )

    if run is not None:
        properties.update(
            {
                "run_id": str(run.id),
                "run_status": run.status.value,
                "has_conversation_id": bool(run.conversation_id),
            }
        )
        properties.setdefault("automation_id", str(run.automation_id))
        if run.started_at and run.completed_at:
            duration_ms = int(
                (run.completed_at - run.started_at).total_seconds() * 1000
            )
            properties["duration_ms"] = duration_ms

    return properties


async def capture_automation_event(
    event: str,
    *,
    request: Request | None = None,
    request_context: TelemetryRequestContext | None = None,
    user: AuthenticatedUser | None = None,
    automation: Automation | None = None,
    run: AutomationRun | None = None,
    properties: dict[str, Any] | None = None,
    session: AsyncSession | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """Capture a sanitized automation product event without affecting callers."""
    settings = get_config().service
    if not settings.posthog_api_key:
        return

    context = request_context or get_request_telemetry_context(request)
    backend_distinct_id = await get_automation_backend_distinct_id(
        request=request,
        session=session,
        session_factory=session_factory,
    )
    if backend_distinct_id is None:
        return

    event_properties = _base_properties(
        request_context=context,
        user=user,
        automation=automation,
        run=run,
        backend_distinct_id=backend_distinct_id,
    )
    if properties:
        event_properties.update(properties)

    payload = {
        "api_key": settings.posthog_api_key,
        "event": event,
        "distinct_id": _resolve_distinct_id(backend_distinct_id=backend_distinct_id),
        "properties": event_properties,
    }

    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.post(
                f"{settings.posthog_host.rstrip('/')}{POSTHOG_CAPTURE_PATH}",
                json=payload,
            )
            response.raise_for_status()
    except Exception:
        logger.debug("Failed to capture automation telemetry event", exc_info=True)
