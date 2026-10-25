"""Best-effort PostHog product telemetry for automation lifecycle events.

Callers capture while their transaction is still open, so capture must not wait
on the network: the PostHog POST runs in a background task, and the backend id
is cached after its first read so a request never checks out a second pooled
connection for it.
"""

import asyncio
import json
import logging
import uuid
from functools import lru_cache
from importlib.metadata import PackageNotFoundError
from typing import Any

import httpx
from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from openhands.automation import __version__
from openhands.automation.auth import AuthenticatedUser
from openhands.automation.config import get_config
from openhands.automation.middleware import (
    TelemetryRequestContext,
    build_telemetry_request_context,
)
from openhands.automation.models import Automation, AutomationRun
from openhands.automation.utils.service_metadata import (
    get_service_metadata,
    set_service_metadata,
)
from openhands.automation.utils.time import ensure_utc, utcnow
from openhands.automation.utils.version import (
    ServerVersionInfo,
    get_sdk_version,
    get_server_version_info,
)


logger = logging.getLogger("automation.telemetry")
_telemetry_http_client: httpx.AsyncClient | None = None
# Write-once in the database, so safe to keep for the process lifetime.
_backend_distinct_id: str | None = None
# Strong references: the event loop only holds weak ones to tasks.
_pending_sends: set[asyncio.Task[None]] = set()
# Bounds memory if PostHog stalls; each send ends within its client timeouts.
MAX_PENDING_SENDS = 1000
SHUTDOWN_FLUSH_TIMEOUT_SECONDS = 2.0
AUTOMATION_BACKEND_ID_PROPERTY = "automation_backend_id"
FRONTEND_DISTINCT_ID_PROPERTY = "frontend_distinct_id"
POSTHOG_CAPTURE_PATH = "/capture/"
TELEMETRY_CONSENT_METADATA_KEY = "posthog_frontend_consent_by_distinct_id"
TELEMETRY_CONSENT_ANONYMOUS_ID = "__anonymous__"

TELEMETRY_BACKEND_DISTINCT_ID_KEY = "posthog_backend_distinct_id"


def get_telemetry_http_client() -> httpx.AsyncClient:
    """Return the process-wide client used for PostHog capture requests."""
    global _telemetry_http_client
    if _telemetry_http_client is None:
        _telemetry_http_client = httpx.AsyncClient(timeout=2.0)
    return _telemetry_http_client


async def flush_telemetry_events(timeout: float | None = None) -> None:
    """Wait for queued PostHog sends, cancelling any still running at ``timeout``."""
    pending = set(_pending_sends)
    if not pending:
        return
    _, unfinished = await asyncio.wait(pending, timeout=timeout)
    for task in unfinished:
        task.cancel()
    if unfinished:
        logger.debug("Dropped %d unsent automation telemetry events", len(unfinished))
        await asyncio.gather(*unfinished, return_exceptions=True)


async def close_telemetry_http_client() -> None:
    """Flush queued sends, then close the shared client for future lifespans."""
    await flush_telemetry_events(timeout=SHUTDOWN_FLUSH_TIMEOUT_SECONDS)
    global _telemetry_http_client
    client = _telemetry_http_client
    _telemetry_http_client = None
    if client is not None:
        await client.aclose()


async def _get_or_create_backend_distinct_id(session: AsyncSession) -> str:
    existing = await get_service_metadata(session, TELEMETRY_BACKEND_DISTINCT_ID_KEY)
    if existing:
        return existing

    generated = f"automation-backend:{uuid.uuid4()}"
    # DO NOTHING, not the DO UPDATE set_service_metadata does: a concurrent
    # first caller's id may already have been handed to other callers.
    await session.execute(
        text(
            "INSERT INTO automation_service_metadata (key, value) "
            "VALUES (:key, :value) ON CONFLICT (key) DO NOTHING"
        ),
        {"key": TELEMETRY_BACKEND_DISTINCT_ID_KEY, "value": generated},
    )
    return (
        await get_service_metadata(session, TELEMETRY_BACKEND_DISTINCT_ID_KEY)
        or generated
    )


async def get_automation_backend_distinct_id(
    *,
    request: Request | None = None,
    session: AsyncSession | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> str | None:
    """Return the DB-backed automation backend PostHog distinct ID."""
    global _backend_distinct_id
    if _backend_distinct_id is not None:
        return _backend_distinct_id

    try:
        if session is not None:
            # Not cached: an id inserted here belongs to the caller's
            # transaction, which may still roll back.
            return await _get_or_create_backend_distinct_id(session)

        if session_factory is None and request is not None:
            session_factory = getattr(request.app.state, "session_factory", None)

        if session_factory is None:
            logger.debug("No database session available for automation telemetry ID")
            return None

        async with session_factory() as new_session:
            distinct_id = await _get_or_create_backend_distinct_id(new_session)
            await new_session.commit()
    except Exception:
        logger.debug("Failed to load automation telemetry backend ID", exc_info=True)
        return None

    _backend_distinct_id = distinct_id
    return distinct_id


def _normalize_frontend_distinct_id(frontend_distinct_id: str | None) -> str:
    normalized = (frontend_distinct_id or "").strip()
    return normalized[:256] or TELEMETRY_CONSENT_ANONYMOUS_ID


def _parse_telemetry_consent_map(raw_value: str | None) -> dict[str, bool]:
    if not raw_value:
        return {}
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        str(frontend_id): consent
        for frontend_id, consent in parsed.items()
        if isinstance(consent, bool)
    }


async def _load_telemetry_consent_map(session: AsyncSession) -> dict[str, bool]:
    raw_value = await get_service_metadata(session, TELEMETRY_CONSENT_METADATA_KEY)
    return _parse_telemetry_consent_map(raw_value)


def has_granted_telemetry_consent(consents: dict[str, bool]) -> bool:
    return any(consents.values())


async def set_stored_telemetry_consent(
    session: AsyncSession,
    *,
    consent_granted: bool,
    frontend_distinct_id: str | None,
) -> bool:
    """Store frontend telemetry consent and return aggregate consent state."""
    consents = await _load_telemetry_consent_map(session)
    consents[_normalize_frontend_distinct_id(frontend_distinct_id)] = consent_granted
    serialized = json.dumps(consents, sort_keys=True)
    await set_service_metadata(session, TELEMETRY_CONSENT_METADATA_KEY, serialized)
    return has_granted_telemetry_consent(consents)


async def get_stored_telemetry_consent(
    *,
    request: Request | None = None,
    session: AsyncSession | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    frontend_distinct_id: str | None = None,
) -> bool:
    """Return aggregate consent or consent for a specific frontend identity."""

    def resolve(consents: dict[str, bool]) -> bool:
        if frontend_distinct_id is None:
            return has_granted_telemetry_consent(consents)
        return consents.get(
            _normalize_frontend_distinct_id(frontend_distinct_id), False
        )

    try:
        if session is not None:
            return resolve(await _load_telemetry_consent_map(session))

        if session_factory is None and request is not None:
            session_factory = getattr(request.app.state, "session_factory", None)

        if session_factory is None:
            logger.debug(
                "No database session available for automation telemetry consent"
            )
            return False

        async with session_factory() as new_session:
            return resolve(await _load_telemetry_consent_map(new_session))
    except Exception:
        logger.debug("Failed to load automation telemetry consent", exc_info=True)
        return False


def get_request_telemetry_context(request: Request | None) -> TelemetryRequestContext:
    if request is None:
        return TelemetryRequestContext()
    context = getattr(request.state, "telemetry_context", None)
    if not isinstance(context, TelemetryRequestContext):
        context = build_telemetry_request_context(request.scope)
    return _trusted_telemetry_context(context)


def _trusted_telemetry_context(
    context: TelemetryRequestContext,
) -> TelemetryRequestContext:
    """Discard browser identity where Cloud must derive identity from auth."""
    if get_config().service.is_local_mode:
        return context
    return TelemetryRequestContext(
        client_source=context.client_source,
        client_version=context.client_version,
    )


def get_request_authenticated_user(request: Request) -> AuthenticatedUser | None:
    user = getattr(request.state, "authenticated_user", None)
    return user if isinstance(user, AuthenticatedUser) else None


def _trigger_type(automation: Automation | None) -> str | None:
    trigger = automation.trigger if automation is not None else None
    if isinstance(trigger, dict):
        value = trigger.get("type")
        return str(value) if value is not None else None
    return str(trigger) if trigger is not None else None


def _resolve_local_frontend_distinct_id(
    *,
    request_context: TelemetryRequestContext,
    automation: Automation | None,
    run: AutomationRun | None,
) -> str | None:
    if run is not None and run.telemetry_distinct_id:
        return run.telemetry_distinct_id
    if request_context.frontend_distinct_id:
        return request_context.frontend_distinct_id
    if automation is not None:
        return automation.telemetry_distinct_id
    return None


def _resolve_distinct_id(
    *,
    request_context: TelemetryRequestContext,
    user: AuthenticatedUser | None,
    automation: Automation | None,
    run: AutomationRun | None,
    backend_distinct_id: str,
) -> str:
    settings = get_config().service
    if not settings.is_local_mode:
        if user is not None:
            return str(user.user_id)
        if automation is not None:
            return str(automation.user_id)
        return backend_distinct_id

    return (
        _resolve_local_frontend_distinct_id(
            request_context=request_context,
            automation=automation,
            run=run,
        )
        or backend_distinct_id
    )


@lru_cache(maxsize=1)
def _installed_sdk_version() -> str:
    # Only successful lookups are cached: lru_cache does not memoize exceptions.
    return get_sdk_version()


def _server_version_properties() -> ServerVersionInfo:
    try:
        sdk_version = _installed_sdk_version()
    except PackageNotFoundError:
        return get_server_version_info(missing_sdk_version="unknown")
    return {"package_version": __version__, "sdk_version": sdk_version}


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
        "deployment_kind": "local" if settings.is_local_mode else "remote",
        "automation_service": "openhands_automation",
        **_server_version_properties(),
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
                    "$groups": {"org": str(automation.org_id)},
                }
            )

    if user is not None and not settings.is_local_mode:
        properties.update(
            {
                "cloud_user_id": str(user.user_id),
                "cloud_org_id": str(user.org_id),
                "$groups": {"org": str(user.org_id)},
            }
        )

    if run is not None:
        properties.update(
            {
                "run_id": str(run.id),
                "run_status": run.status.value if run.status is not None else None,
                "has_conversation_id": bool(run.conversation_id),
            }
        )
        properties.setdefault("automation_id", str(run.automation_id))
        if run.started_at and run.completed_at:
            duration_ms = int(
                (
                    ensure_utc(run.completed_at) - ensure_utc(run.started_at)
                ).total_seconds()
                * 1000
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
    """Capture a sanitized automation product event without affecting callers.

    The event is built from the arguments now and queued; the PostHog POST
    happens in the background, so callers never wait on it.
    """
    try:
        await _capture_automation_event(
            event,
            request=request,
            request_context=request_context,
            user=user,
            automation=automation,
            run=run,
            properties=properties,
            session=session,
            session_factory=session_factory,
        )
    except Exception:
        logger.debug("Failed to capture automation telemetry event", exc_info=True)


async def _capture_automation_event(
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
    settings = get_config().service
    if not settings.posthog_api_key:
        return

    context = (
        _trusted_telemetry_context(request_context)
        if request_context is not None
        else get_request_telemetry_context(request)
    )
    local_frontend_distinct_id = (
        _resolve_local_frontend_distinct_id(
            request_context=context,
            automation=automation,
            run=run,
        )
        if settings.is_local_mode
        else None
    )
    if settings.is_local_mode and not await get_stored_telemetry_consent(
        request=request,
        session=session,
        session_factory=session_factory,
        frontend_distinct_id=local_frontend_distinct_id,
    ):
        return

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
        "distinct_id": _resolve_distinct_id(
            request_context=context,
            user=user,
            automation=automation,
            run=run,
            backend_distinct_id=backend_distinct_id,
        ),
        "properties": event_properties,
        # Sends run concurrently, so arrival order is not event order.
        "timestamp": utcnow().isoformat(),
    }

    if len(_pending_sends) >= MAX_PENDING_SENDS:
        logger.debug("Dropped automation telemetry event %s: send queue full", event)
        return
    task = asyncio.create_task(
        _send_event(
            f"{settings.posthog_host.rstrip('/')}{POSTHOG_CAPTURE_PATH}", payload
        )
    )
    _pending_sends.add(task)
    task.add_done_callback(_pending_sends.discard)


async def _send_event(url: str, payload: dict[str, Any]) -> None:
    try:
        response = await get_telemetry_http_client().post(url, json=payload)
        response.raise_for_status()
    except Exception:
        logger.debug("Failed to send automation telemetry event", exc_info=True)
