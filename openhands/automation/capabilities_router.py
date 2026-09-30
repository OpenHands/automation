"""Capability discovery and preflight validation for automation setup UIs.

A setup UI needs two answers before it creates anything: what this deployment
supports, so it never offers an option that cannot work, and whether a draft is
acceptable, so a bad configuration is caught before an automation exists.
Neither endpoint writes.
"""

import asyncio
import fnmatch
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import Any, Final, Literal
from urllib.parse import quote, urlparse
from zoneinfo import available_timezones

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.auth import (
    MAX_SESSION_COOKIE_CHUNKS,
    SESSION_COOKIE_NAME,
    X_ORG_ID_HEADER,
    AuthenticatedUser,
    AuthMethod,
    get_http_client,
    require_permission,
)
from openhands.automation.config import get_config
from openhands.automation.db import get_session
from openhands.automation.draft_schemas import (
    FINAL_DRAFT_MODELS,
    DraftModel,
    normalize_draft_body,
)
from openhands.automation.event_schemas import parse_event
from openhands.automation.event_schemas.github import (
    get_supported_event_patterns,
    get_supported_event_types,
)
from openhands.automation.filter_eval import FilterFunctions
from openhands.automation.models import CustomWebhook
from openhands.automation.providers import builtin_sources
from openhands.automation.scheduler import POLL_INTERVAL_SECONDS
from openhands.automation.schemas import (
    CapabilitiesResponse,
    CreateAutomationRequest,
    CronCapabilities,
    CronTrigger,
    DraftValidationError,
    EventCapabilities,
    EventTrigger,
    PreflightIntegrationAlternative,
    PreflightIntegrationRequirement,
    TriggerCapabilities,
    ValidateDraftRequest,
    ValidateDraftResponse,
)
from openhands.automation.trigger_matcher import matches_trigger
from openhands.automation.utils.cron import min_interval_seconds
from openhands.automation.utils.model_profiles import (
    validate_agent_profile_selection,
    validate_model_profile_for_user,
)
from openhands.automation.utils.webhook import get_webhook_config
from openhands.sdk.settings import OpenHandsAgentSettings
from openhands.sdk.workspace.repo import PROVIDER_TOKEN_NAMES, GitProvider, RepoSource


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["Capabilities"])

_require_view_automations = require_permission("view_automations")

# Features every deployment has: they come from the SDK code the service
# packages into a run, not from configuration.
_STATIC_FEATURES: Final[tuple[str, ...]] = (
    "automationDrafts",
    "conversationDispatch",
    # Can run a client-supplied tarball, so an entry may ship a script bundle.
    "customTarball",
    "mcpTools",
    "presetPlugin",
    "presetPrompt",
    "repoClone",
)

# Tags Pydantic inserts into an error location for the trigger union.
_TRIGGER_TAGS: Final[frozenset[str]] = frozenset({"cron", "event"})

# Leave time for the server to return its verdict after its own probe deadline.
_MCP_PROBE_TIMEOUT_SECONDS: Final[float] = 15.0
_MCP_PROBE_REQUEST_TIMEOUT_SECONDS: Final[float] = _MCP_PROBE_TIMEOUT_SECONDS + 5.0
_PREFLIGHT_TOTAL_TIMEOUT_SECONDS: Final[float] = 60.0
_MAX_PREFLIGHT_SEARCH_PAGES: Final[int] = 10
_PREFLIGHT_CONCURRENCY: Final[int] = 4
_MAX_PREFLIGHT_REPOSITORIES: Final[int] = 32
# The Cloud git route requests one look-ahead item from providers to decide
# whether to return ``next_page_id``. Keeping this below provider max 100 lets
# that look-ahead survive instead of being clamped away.
_CLOUD_REPOSITORY_SEARCH_PAGE_SIZE: Final[int] = 99
# The Cloud API targets these public hosts. An explicit provider must not make
# a self-hosted or lookalike URL pass by checking the same path on a public host.
_REPOSITORY_PROVIDER_HOSTS: Final[dict[str, str]] = {
    "github": "github.com",
    "gitlab": "gitlab.com",
    "bitbucket": "bitbucket.org",
}


class _DependencyUnavailable(Exception):
    """Internal control flow for an inconclusive check, translated to a safe 503.

    Keep this private: callers consume HTTP verdicts, never dependency exceptions.
    The exception deliberately carries no upstream body, URL, or credentials.
    """


@dataclass(frozen=True, slots=True)
class _PreflightTarget:
    """Trusted service URL and request-scoped authentication for all probes."""

    base_url: str
    headers: dict[str, str]
    local: bool


@dataclass(frozen=True, slots=True)
class _StoredMCPServer:
    """Normalized matching metadata and the config used only by local probes."""

    name: str
    raw: dict[str, Any]
    transport: Literal["stdio", "shttp", "sse"]
    locator: str
    auth_strategy: str
    enabled: bool


@router.get("/capabilities", response_model_exclude_none=True)
async def get_capabilities(
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session),
) -> CapabilitiesResponse:
    """Describe what this deployment supports, before anything is configured.

    Custom webhook sources are organization-scoped, so this response describes
    what the calling organization can do here.
    """
    config = get_config()

    # In cloud mode every run needs a minted API key, which needs the service
    # key. Without it the service accepts work it cannot execute, so it offers
    # nothing rather than letting a setup form proceed into certain failure.
    if not (config.service.is_local_mode or config.service.service_key):
        return CapabilitiesResponse(
            ready=False,
            max_automation_timeout_seconds=config.sandbox.max_run_duration,
            trigger_kinds=[],
            event_sources=[],
            event_types=[],
            triggers=TriggerCapabilities(),
            features=[],
        )

    builtin = builtin_sources() if config.service.webhook_secret else []
    event_sources = sorted({*builtin, *await _custom_sources(user.org_id, session)})

    features = [*_STATIC_FEATURES]
    if config.service.is_local_mode:
        features.append("agentProfiles")
    if event_sources:
        features.append("webhookDelivery")
    if config.kv.enabled:
        features.append("kvStore")

    return CapabilitiesResponse(
        ready=True,
        max_automation_timeout_seconds=config.sandbox.max_run_duration,
        trigger_kinds=["cron", "event"] if event_sources else ["cron"],
        event_sources=event_sources,
        event_types=(get_supported_event_patterns() if "github" in builtin else []),
        triggers=TriggerCapabilities(
            cron=CronCapabilities(
                min_interval_seconds=_cron_interval_floor(),
                timezones=sorted(available_timezones()),
            ),
            event=(
                EventCapabilities(
                    filter_functions=sorted(FilterFunctions.FUNCTION_TABLE)
                )
                if event_sources
                else None
            ),
        ),
        features=sorted(features),
    )


@router.post("/validate")
async def validate_draft(
    body: ValidateDraftRequest,
    request: Request,
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session),
    client: httpx.AsyncClient = Depends(get_http_client),
) -> ValidateDraftResponse:
    """Validate a draft automation without creating it.

    An invalid draft is still a successful validation: the response is 200 with
    `valid` false and one error per problem, each addressed to the field that
    caused it. Unsupported checks return 501 only after other checks succeed;
    dependency failures return 503. Only a malformed request envelope is a 4xx.
    """
    logger.info(
        "Validating draft for %s (automation_id=%s)", body.endpoint, body.automation_id
    )

    try:
        normalized_draft = normalize_draft_body(body.endpoint, body.draft)
        draft = FINAL_DRAFT_MODELS[body.endpoint].model_validate(normalized_draft)
    except ValidationError as e:
        return ValidateDraftResponse(valid=False, errors=_schema_errors(e))

    try:
        async with asyncio.timeout(_PREFLIGHT_TOTAL_TIMEOUT_SECONDS):
            return await _validated_draft_response(
                body=body,
                draft=draft,
                request=request,
                user=user,
                session=session,
                client=client,
            )
    except (TimeoutError, _DependencyUnavailable):
        logger.warning("A preflight validation dependency is unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Preflight validation is temporarily unavailable.",
        ) from None


async def _validated_draft_response(
    *,
    body: ValidateDraftRequest,
    draft: DraftModel,
    request: Request,
    user: AuthenticatedUser,
    session: AsyncSession,
    client: httpx.AsyncClient,
) -> ValidateDraftResponse:
    """Collect draft errors; deployment checks are opt-in for legacy clients."""
    errors: list[DraftValidationError] = []
    sample_event_matched: bool | None = None

    try:
        validate_model_profile_for_user(draft.model, user)
    except HTTPException as e:
        errors.append(
            DraftValidationError(
                field="model",
                code="model_profile_not_found",
                message=str(e.detail),
            )
        )

    if isinstance(draft, CreateAutomationRequest):
        try:
            validate_agent_profile_selection(draft.agent_profile_id, draft.model)
        except HTTPException as e:
            errors.append(
                DraftValidationError(
                    field="agent_profile_id",
                    code="invalid_agent_profile",
                    message=str(e.detail),
                )
            )

    trigger = draft.trigger
    if isinstance(trigger, CronTrigger):
        errors.extend(_cron_errors(trigger))
    elif isinstance(trigger, EventTrigger):
        webhook = await get_webhook_config(trigger.source, user.org_id, session)
        if webhook is None:
            errors.append(
                DraftValidationError(
                    field="trigger.source",
                    code="event_source_not_configured",
                    message=(
                        f"No webhook is configured to deliver '{trigger.source}' "
                        "events to this deployment."
                    ),
                )
            )
        else:
            errors.extend(_event_type_errors(trigger))
            if body.sample_event is not None:
                try:
                    event = parse_event(
                        trigger.source,
                        body.sample_event,
                        event_key_expr=webhook.event_key_expr,
                    )
                except ValueError as e:
                    errors.append(
                        DraftValidationError(
                            field="sampleEvent",
                            code="unparseable_sample_event",
                            message=str(e),
                        )
                    )
                else:
                    sample_event_matched = matches_trigger(
                        trigger, trigger.source, event.event_key, body.sample_event
                    )

    if body.requirements is not None:
        errors.extend(
            await _deployment_preflight_errors(
                body=body,
                draft=draft,
                user=user,
                request=request,
                client=client,
            )
        )

    # Complete every check before selecting the existing advisory response.
    # An unsupported ref must not mask a missing credential, a denied repo, or
    # an invalid trigger. Dependency failures have already propagated as 503.
    unsupported_refs = [
        error for error in errors if error.code == "repository_ref_unverified"
    ]
    errors = [error for error in errors if error.code != "repository_ref_unverified"]
    if unsupported_refs and not errors:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=unsupported_refs[0].message,
        )

    return ValidateDraftResponse(
        valid=not errors,
        errors=errors,
        sample_event_matched=sample_event_matched,
    )


async def _deployment_preflight_errors(
    *,
    body: ValidateDraftRequest,
    draft: DraftModel,
    user: AuthenticatedUser,
    request: Request,
    client: httpx.AsyncClient,
) -> list[DraftValidationError]:
    """Load shared metadata once, then check independent requirements in parallel."""
    requirements = body.requirements
    if requirements is None:
        return []

    repos = [] if isinstance(draft, CreateAutomationRequest) else (draft.repos or [])
    if len(repos) > _MAX_PREFLIGHT_REPOSITORIES:
        return [
            DraftValidationError(
                field="repos",
                code="too_many_repositories",
                message=(
                    "Validate at most "
                    f"{_MAX_PREFLIGHT_REPOSITORIES} repositories at once."
                ),
            )
        ]
    if not requirements.integrations and not repos:
        return []

    target = _preflight_target(request, user)
    requested_secret_names = {
        name
        for requirement in requirements.integrations
        for alternative in requirement.alternatives
        for name in alternative.secret_names
    }
    if target.local and repos:
        requested_secret_names.update(PROVIDER_TOKEN_NAMES.values())

    available_secret_names = await _available_secret_names(
        target,
        requested_secret_names,
        client,
    )
    checks: list[Callable[[], Awaitable[list[DraftValidationError]]]] = []
    if requirements.integrations:
        servers = await _stored_mcp_servers(target, client)
        for requirement in requirements.integrations:
            checks.append(
                partial(
                    _integration_errors,
                    requirement,
                    servers,
                    available_secret_names,
                    target,
                    client,
                )
            )

    for index, repository in enumerate(repos):
        checks.append(
            partial(
                _repository_errors,
                repository=repository,
                index=index,
                available_secret_names=available_secret_names,
                target=target,
                client=client,
            )
        )
    return await _run_preflight_checks(checks)


async def _run_preflight_checks(
    checks: list[Callable[[], Awaitable[list[DraftValidationError]]]],
) -> list[DraftValidationError]:
    """Limit outbound work and retain field order, even when responses race.

    Gather alone leaves siblings running after a failure. Always cancel and
    drain them before returning, including when the request deadline expires.
    Factories keep queued checks from creating unawaited coroutines on cancel.
    """
    limit = asyncio.Semaphore(_PREFLIGHT_CONCURRENCY)

    async def run(
        check: Callable[[], Awaitable[list[DraftValidationError]]],
    ) -> list[DraftValidationError]:
        async with limit:
            return await check()

    tasks = [asyncio.create_task(run(check)) for check in checks]
    try:
        results = await asyncio.gather(*tasks)
        return [error for errors in results for error in errors]
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _preflight_target(request: Request, user: AuthenticatedUser) -> _PreflightTarget:
    """Probe under the same effective identity and organization as creation."""
    settings = get_config().service
    if settings.is_local_mode:
        return _PreflightTarget(
            base_url=settings.agent_server_url.rstrip("/"),
            headers=(
                {"X-Session-API-Key": settings.agent_server_api_key}
                if settings.agent_server_api_key
                else {}
            ),
            local=True,
        )

    # Authentication already resolved API-key scope > selected org > current org.
    # Reuse that identity instead of allowing a later Cloud read to choose another.
    headers = {X_ORG_ID_HEADER: str(user.org_id)}
    if user.auth_method == AuthMethod.API_KEY and user.api_key:
        headers["Authorization"] = f"Bearer {user.api_key}"
        return _PreflightTarget(
            settings.openhands_api_base_url.rstrip("/"), headers, False
        )
    if user.auth_method == AuthMethod.COOKIE:
        cookies: list[str] = []
        for index in range(MAX_SESSION_COOKIE_CHUNKS):
            name = (
                SESSION_COOKIE_NAME if index == 0 else f"{SESSION_COOKIE_NAME}_{index}"
            )
            value = request.cookies.get(name)
            if value is None:
                break
            cookies.append(f"{name}={value}")
        if not cookies:
            raise _DependencyUnavailable
        headers["Cookie"] = "; ".join(cookies)
        return _PreflightTarget(
            settings.openhands_api_base_url.rstrip("/"), headers, False
        )
    raise _DependencyUnavailable


async def _send_preflight_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    **kwargs: Any,
) -> httpx.Response:
    """Separate inconclusive dependency failures from actionable validation errors."""
    try:
        response = await client.request(method, url, headers=headers, **kwargs)
    except httpx.RequestError:
        # HTTP exceptions may retain credential-bearing URLs or provider details.
        # Suppress their chain at this boundary as well as the public response.
        raise _DependencyUnavailable from None
    if (
        response.status_code == status.HTTP_429_TOO_MANY_REQUESTS
        or response.status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR
    ):
        raise _DependencyUnavailable
    return response


def _json_object(response: httpx.Response) -> dict[str, Any]:
    """Reject HTML proxy errors and incompatible JSON without exposing their bodies."""
    try:
        value = response.json()
    except ValueError:
        raise _DependencyUnavailable from None
    if not isinstance(value, dict):
        raise _DependencyUnavailable
    return value


async def _available_secret_names(
    target: _PreflightTarget,
    requested_names: set[str],
    client: httpx.AsyncClient,
) -> set[str]:
    """Read secret metadata only; a search match must equal the requested name."""
    if not requested_names:
        return set()

    if target.local:
        response = await _send_preflight_request(
            client,
            "GET",
            f"{target.base_url}/api/settings/secrets",
            headers=target.headers,
        )
        if response.status_code != status.HTTP_200_OK:
            raise _DependencyUnavailable
        names = set(_string_items(_json_object(response), "secrets", "name"))
        return names.intersection(requested_names)

    found: set[str] = set()
    for name in sorted(requested_names):
        if await _cloud_secret_exists(target, name, client):
            found.add(name)
    return found


def _string_items(data: dict[str, Any], collection: str, field: str) -> list[str]:
    """Validate the metadata we consume; upstream JSON is not a typed model.

    A missing or malformed collection is not evidence that a credential or repo
    is absent. Treat an incompatible response as unavailable, not an empty list.
    """
    items = data.get(collection)
    if not isinstance(items, list):
        raise _DependencyUnavailable
    values: list[str] = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get(field), str):
            raise _DependencyUnavailable
        values.append(item[field])
    return values


def _next_page_id(data: dict[str, Any], seen: set[str]) -> str | None:
    """Accept a fresh cursor or normal end-of-results; reject broken pagination."""
    page_id = data.get("next_page_id")
    if page_id is None:
        return None
    if not isinstance(page_id, str) or not page_id or page_id in seen:
        raise _DependencyUnavailable
    seen.add(page_id)
    return page_id


async def _cloud_secret_exists(
    target: _PreflightTarget, name: str, client: httpx.AsyncClient
) -> bool:
    """Resolve an exact secret name through Cloud's paginated substring search."""
    params: dict[str, str | int] = {"name__contains": name, "limit": 100}
    seen: set[str] = set()
    for _ in range(_MAX_PREFLIGHT_SEARCH_PAGES):
        response = await _send_preflight_request(
            client,
            "GET",
            f"{target.base_url}/api/v1/secrets/search",
            headers=target.headers,
            params=params,
        )
        if response.status_code != status.HTTP_200_OK:
            raise _DependencyUnavailable
        data = _json_object(response)
        if name in _string_items(data, "items", "name"):
            return True
        page_id = _next_page_id(data, seen)
        if page_id is None:
            return False
        params["page_id"] = page_id
    # A truncated search cannot prove absence. Do not ask the user to replace
    # a credential that may simply be beyond the service's bounded search.
    raise _DependencyUnavailable


async def _stored_mcp_servers(
    target: _PreflightTarget,
    client: httpx.AsyncClient,
) -> list[_StoredMCPServer]:
    """Load stored servers, isolating invalid entries from unrelated integrations."""
    headers = dict(target.headers)
    path = "/api/settings" if target.local else "/api/v1/settings"
    if target.local:
        headers["X-Expose-Secrets"] = "encrypted"
    response = await _send_preflight_request(
        client,
        "GET",
        f"{target.base_url}{path}",
        headers=headers,
    )
    if response.status_code != status.HTTP_200_OK:
        raise _DependencyUnavailable
    agent_settings = _json_object(response).get("agent_settings")
    if not isinstance(agent_settings, dict):
        raise _DependencyUnavailable
    raw_config = agent_settings.get("mcp_config", {})
    if isinstance(raw_config, dict) and "mcpServers" in raw_config:
        raw_config = raw_config["mcpServers"]
    if not isinstance(raw_config, dict):
        raise _DependencyUnavailable

    servers: list[_StoredMCPServer] = []
    for name, raw in raw_config.items():
        if not isinstance(name, str) or not isinstance(raw, dict):
            continue
        try:
            servers.append(_parse_stored_mcp_server(name, raw))
        except _DependencyUnavailable:
            continue
    return servers


def _parse_stored_mcp_server(name: str, raw: dict[str, Any]) -> _StoredMCPServer:
    """Use the SDK's auth migration so old saved settings still match the catalog."""
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise _DependencyUnavailable

    raw_transport = raw.get("transport", raw.get("type"))
    if raw_transport is None:
        raw_transport = "stdio" if isinstance(raw.get("command"), str) else "http"
    if not isinstance(raw_transport, str):
        raise _DependencyUnavailable
    transport: Literal["stdio", "shttp", "sse"]
    match raw_transport:
        case "stdio":
            transport, locator = "stdio", name
        case "http" | "streamable-http" | "shttp":
            transport, locator = "shttp", raw.get("url")
        case "sse":
            transport, locator = "sse", raw.get("url")
        case _:
            raise _DependencyUnavailable
    if not isinstance(locator, str) or not locator:
        raise _DependencyUnavailable

    try:
        server = OpenHandsAgentSettings.from_persisted(
            {"mcp_config": {name: raw}}
        ).mcp_config[name]
    except (TypeError, ValueError):
        raise _DependencyUnavailable from None
    auth_strategy = server.auth.strategy if server.auth is not None else "none"
    return _StoredMCPServer(
        name=name,
        # Local settings were fetched with encrypted exposure. Preserve those
        # opaque values for agent-server to decrypt, never return them to the UI.
        raw=server.model_dump(
            mode="json",
            exclude_none=True,
            exclude_defaults=True,
            context={"expose_secrets": "plaintext"},
        ),
        transport=transport,
        locator=locator,
        auth_strategy=auth_strategy,
        enabled=enabled,
    )


def _normalized_remote_locator(
    value: str,
) -> tuple[str, str, int | None, str, str] | None:
    """Normalize URL spelling without merging different query-selected tenants."""
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port == 0
    ):
        return None
    if (parsed.scheme == "http" and port == 80) or (
        parsed.scheme == "https" and port == 443
    ):
        port = None
    return (
        parsed.scheme.lower(),
        parsed.hostname.lower(),
        port,
        parsed.path.rstrip("/"),
        parsed.query,
    )


def _alternative_matches_server(
    alternative: PreflightIntegrationAlternative,
    server: _StoredMCPServer,
) -> bool:
    """Match the stored connection, not an arbitrary URL supplied for a probe."""
    if alternative.transport != server.transport:
        return False
    if alternative.transport == "stdio" and alternative.locator != server.locator:
        return False
    if alternative.transport != "stdio":
        locator = _normalized_remote_locator(alternative.locator)
        if locator is None or locator != _normalized_remote_locator(server.locator):
            return False

    expected_auth = alternative.auth_strategy
    if expected_auth is None or (
        server.auth_strategy == "header" and expected_auth == "none"
    ):
        return True
    return server.auth_strategy == expected_auth


async def _integration_errors(
    requirement: PreflightIntegrationRequirement,
    servers: list[_StoredMCPServer],
    available_secret_names: set[str],
    target: _PreflightTarget,
    client: httpx.AsyncClient,
) -> list[DraftValidationError]:
    """Any usable alternative satisfies an integration; report missing prerequisites.

    Try another matching server after a probe failure. If none succeeds and a
    dependency failed, we cannot distinguish an outage from a bad connection.
    """
    matches = [
        (alternative, server)
        for alternative in requirement.alternatives
        for server in servers
        if _alternative_matches_server(alternative, server)
    ]
    enabled_matches = [(alt, server) for alt, server in matches if server.enabled]
    if not enabled_matches:
        code = "integration_disabled" if matches else "integration_not_configured"
        message = (
            f"Enable the configured {requirement.id} connection before continuing."
            if matches
            else f"Connect {requirement.id} before continuing."
        )
        return [
            DraftValidationError(
                field=None,
                code=code,
                message=message,
                step="prerequisites",
            )
        ]

    missing_names: list[str] = []
    probe_candidates: list[_StoredMCPServer] = []
    for alternative, server in enabled_matches:
        missing = [
            name
            for name in alternative.secret_names
            if name not in available_secret_names
        ]
        if missing:
            for name in missing:
                if name not in missing_names:
                    missing_names.append(name)
            continue
        if all(candidate.name != server.name for candidate in probe_candidates):
            probe_candidates.append(server)

    if not probe_candidates:
        return [
            DraftValidationError(
                field=None,
                code="credential_missing",
                message=f"Add the required credential '{name}' before continuing.",
                step="prerequisites",
            )
            for name in missing_names
        ]

    dependency_failed = False
    for server in probe_candidates:
        try:
            if await _probe_mcp_server(target, server, client):
                return []
        except _DependencyUnavailable:
            dependency_failed = True
    if dependency_failed:
        raise _DependencyUnavailable
    return [
        DraftValidationError(
            field=None,
            code="integration_unavailable",
            message=(
                f"The {requirement.id} connection could not be reached. "
                "Reconnect it and try again."
            ),
            step="prerequisites",
        )
    ]


async def _probe_mcp_server(
    target: _PreflightTarget,
    server: _StoredMCPServer,
    client: httpx.AsyncClient,
) -> bool:
    """Delegate connectivity to the service that owns the stored MCP credentials."""
    if target.local:
        path = "/api/mcp/test"
        payload = {
            "name": server.name,
            "server": server.raw,
            "timeout": _MCP_PROBE_TIMEOUT_SECONDS,
        }
    else:
        path = f"/api/v1/settings/mcp/{quote(server.name, safe='')}/test"
        payload = {"timeout": _MCP_PROBE_TIMEOUT_SECONDS}
    response = await _send_preflight_request(
        client,
        "POST",
        f"{target.base_url}{path}",
        headers=target.headers,
        json=payload,
        timeout=_MCP_PROBE_REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code != status.HTTP_200_OK:
        raise _DependencyUnavailable
    ok = _json_object(response).get("ok")
    if not isinstance(ok, bool):
        raise _DependencyUnavailable
    return ok


def _repository_parts(repository: RepoSource) -> tuple[str, str] | None:
    """Accept public-provider identifiers without forwarding URL credentials."""
    try:
        provider = repository.get_provider().value
    except ValueError:
        return None

    raw_url = repository.url
    if raw_url.startswith("git@"):
        user_host, separator, identifier = raw_url.partition(":")
        if not separator or "@" not in user_host:
            return None
        host = user_host.rsplit("@", 1)[1].lower()
        if host != _REPOSITORY_PROVIDER_HOSTS[provider]:
            return None
    elif "://" in raw_url:
        try:
            parsed = urlparse(raw_url)
            host = parsed.hostname
            port = parsed.port
        except ValueError:
            return None
        if (
            parsed.scheme != "https"
            or host is None
            or host.lower() != _REPOSITORY_PROVIDER_HOSTS[provider]
            or port is not None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            return None
        identifier = parsed.path
    else:
        identifier = raw_url
    identifier = identifier.strip("/")
    if identifier.endswith(".git"):
        identifier = identifier[:-4]
    if not identifier or "/" not in identifier:
        return None
    return provider, identifier


async def _repository_errors(
    *,
    repository: RepoSource,
    index: int,
    available_secret_names: set[str],
    target: _PreflightTarget,
    client: httpx.AsyncClient,
) -> list[DraftValidationError]:
    """Select the credential-owning service and preserve the draft's field index."""
    parts = _repository_parts(repository)
    if parts is None:
        return [
            DraftValidationError(
                field=f"repos[{index}].url",
                code="repository_provider_unsupported",
                message="Choose a repository from a supported Git provider.",
            )
        ]
    provider, identifier = parts
    if target.local:
        return await _local_repository_errors(
            provider=provider,
            identifier=identifier,
            ref=repository.ref or None,
            index=index,
            available_secret_names=available_secret_names,
            target=target,
            client=client,
        )
    return await _cloud_repository_errors(
        provider=provider,
        identifier=identifier,
        ref=repository.ref or None,
        index=index,
        target=target,
        client=client,
    )


async def _local_repository_errors(
    *,
    provider: str,
    identifier: str,
    ref: str | None,
    index: int,
    available_secret_names: set[str],
    target: _PreflightTarget,
    client: httpx.AsyncClient,
) -> list[DraftValidationError]:
    """Use the same secret store and name as the local preset's clone path.

    RemoteWorkspace.clone_repos() calls _get_secret_value() with the canonical
    provider name. That reads /api/settings/secrets/{name}, backed by the same
    FileSecretsStore as the metadata list and the repository probe. The clone
    path does not read OPENHANDS_*_TOKEN environment variables or MCP aliases.
    """
    canonical_name = PROVIDER_TOKEN_NAMES[GitProvider(provider)]
    # Missing clone credentials still allow an anonymous check for public repos.
    candidate_names = (
        [canonical_name] if canonical_name in available_secret_names else []
    )

    response = await _send_preflight_request(
        client,
        "POST",
        f"{target.base_url}/api/git/validate-repository",
        headers=target.headers,
        json={
            "provider": provider,
            "repository": identifier,
            "ref": ref,
            "credential_names": candidate_names,
        },
    )
    if response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT:
        return [
            DraftValidationError(
                field=f"repos[{index}].ref" if ref else f"repos[{index}].url",
                code="repository_reference_invalid",
                message="Use a valid repository and Git reference.",
            )
        ]
    if response.status_code != status.HTTP_200_OK:
        raise _DependencyUnavailable
    verdict = _json_object(response).get("status")
    if verdict == "accessible":
        return []
    if verdict == "unavailable":
        raise _DependencyUnavailable
    if verdict == "missing_credentials":
        return [
            DraftValidationError(
                field=f"repos[{index}].url",
                code="repository_credentials_missing",
                message=f"Add credentials that can access {identifier}.",
            )
        ]
    if verdict == "denied":
        code = "repository_access_denied"
        message = f"Your {provider} connection cannot access {identifier}."
    elif verdict == "not_found":
        code = "repository_not_accessible"
        message = f"The repository {identifier} could not be accessed."
    else:
        raise _DependencyUnavailable
    return [
        DraftValidationError(
            field=f"repos[{index}].url",
            code=code,
            message=message,
        )
    ]


async def _cloud_repository_errors(
    *,
    provider: str,
    identifier: str,
    ref: str | None,
    index: int,
    target: _PreflightTarget,
    client: httpx.AsyncClient,
) -> list[DraftValidationError]:
    """Check repository visibility using Cloud's provider credentials, not ours."""
    repository_found = False
    page_id: str | None = None
    seen_page_ids: set[str] = set()
    for _ in range(_MAX_PREFLIGHT_SEARCH_PAGES):
        params: dict[str, str | int] = {
            "provider": provider,
            "query": identifier,
            "limit": _CLOUD_REPOSITORY_SEARCH_PAGE_SIZE,
        }
        if page_id is not None:
            params["page_id"] = page_id
        response = await _send_preflight_request(
            client,
            "GET",
            f"{target.base_url}/api/v1/git/repositories/search",
            headers=target.headers,
            params=params,
        )
        if response.status_code == status.HTTP_403_FORBIDDEN:
            return [
                DraftValidationError(
                    field=f"repos[{index}].url",
                    code="repository_provider_not_connected",
                    message=f"Connect {provider} before choosing a repository.",
                )
            ]
        if response.status_code != status.HTTP_200_OK:
            raise _DependencyUnavailable
        data = _json_object(response)
        names = _string_items(data, "items", "full_name")
        if identifier.casefold() in {name.casefold() for name in names}:
            repository_found = True
            break
        next_page_id = _next_page_id(data, seen_page_ids)
        if next_page_id is None:
            break
        page_id = next_page_id
    else:
        raise _DependencyUnavailable

    if not repository_found:
        return [
            DraftValidationError(
                field=f"repos[{index}].url",
                code="repository_not_accessible",
                message=f"The repository {identifier} could not be accessed.",
            )
        ]
    if ref is None:
        return []
    return await _cloud_ref_errors(
        provider=provider,
        identifier=identifier,
        ref=ref,
        index=index,
        target=target,
        client=client,
    )


async def _cloud_ref_errors(
    *,
    provider: str,
    identifier: str,
    ref: str,
    index: int,
    target: _PreflightTarget,
    client: httpx.AsyncClient,
) -> list[DraftValidationError]:
    """Prove exact branch names; report other refs as unsupported, not invalid.

    Cloud has no tag/arbitrary-commit lookup here. A SHA-prefix match cannot
    disambiguate a hex-named tag or branch, so it is not evidence of validity.
    A completed search miss becomes advisory after all blocking checks finish.
    """

    ref_matches = False
    page_id: str | None = None
    seen_page_ids: set[str] = set()
    for _ in range(_MAX_PREFLIGHT_SEARCH_PAGES):
        params: dict[str, str | int] = {
            "provider": provider,
            "repository": identifier,
            # Cloud only paginates an empty query; a filtered second page is 400.
            "query": "",
            "limit": _CLOUD_REPOSITORY_SEARCH_PAGE_SIZE,
        }
        if page_id is not None:
            params["page_id"] = page_id
        response = await _send_preflight_request(
            client,
            "GET",
            f"{target.base_url}/api/v1/git/branches/search",
            headers=target.headers,
            params=params,
        )
        if response.status_code == status.HTTP_403_FORBIDDEN:
            return [
                DraftValidationError(
                    field=f"repos[{index}].url",
                    code="repository_provider_not_connected",
                    message=f"Reconnect {provider} to check this repository.",
                )
            ]
        if response.status_code != status.HTTP_200_OK:
            raise _DependencyUnavailable
        data = _json_object(response)
        names = _string_items(data, "items", "name")
        ref_matches = ref in names
        if ref_matches:
            break
        next_page_id = _next_page_id(data, seen_page_ids)
        if next_page_id is None:
            break
        page_id = next_page_id
    else:
        raise _DependencyUnavailable
    if ref_matches:
        return []
    return [
        DraftValidationError(
            field=f"repos[{index}].ref",
            code="repository_ref_unverified",
            message=(
                "This deployment cannot verify tags or arbitrary commits. "
                "Review the pinned Git reference before creating the automation."
            ),
        )
    ]


async def _custom_sources(org_id: uuid.UUID, session: AsyncSession) -> list[str]:
    """Custom webhook sources this organization has enabled.

    These carry their own per-webhook secret, so they work whether or not the
    service-level webhook secret for built-in sources is configured.
    """
    result = await session.execute(
        select(CustomWebhook.source)
        .where(CustomWebhook.org_id == org_id, CustomWebhook.enabled.is_(True))
        .distinct()
    )
    return list(result.scalars().all())


def _cron_interval_floor() -> int:
    """Shortest interval between fires the scheduler can actually honour."""
    return max(POLL_INTERVAL_SECONDS, get_config().service.scheduler_interval_seconds)


def _cron_errors(trigger: CronTrigger) -> list[DraftValidationError]:
    """Reject a schedule that asks to fire faster than the scheduler polls."""
    floor = _cron_interval_floor()
    if min_interval_seconds(trigger.schedule) >= floor:
        return []
    return [
        DraftValidationError(
            field="trigger.schedule",
            code="interval_too_short",
            message=(
                "This deployment fires an automation at most once every "
                f"{floor} seconds."
            ),
        )
    ]


def _event_type_errors(trigger: EventTrigger) -> list[DraftValidationError]:
    """Reject event keys this deployment cannot parse.

    Only sources publishing a known catalog can be checked. A custom webhook's
    event keys are whatever its own expression extracts, so anything matches.
    """
    if trigger.source != "github":
        return []

    supported = get_supported_event_types()
    return [
        DraftValidationError(
            field="trigger.on",
            code="event_type_not_delivered",
            message=f"This deployment cannot parse '{pattern}' events from GitHub.",
        )
        for pattern in trigger.event_patterns
        if not any(
            # Compare event types with the pattern's own wildcards, the way the
            # trigger matcher compares a delivered event key.
            fnmatch.fnmatch(event_type, pattern.split(".", 1)[0])
            for event_type in supported
        )
    ]


def _schema_errors(error: ValidationError) -> list[DraftValidationError]:
    """Translate Pydantic's report into field-addressed errors."""
    return [
        DraftValidationError(
            field=_error_field(item["loc"]),
            code=item["type"],
            message=item["msg"],
        )
        for item in error.errors()
    ]


def _error_field(loc: tuple[int | str, ...]) -> str | None:
    """Render a Pydantic error location as a dotted path into the draft.

    List positions become ``repos[0]``, and the tag Pydantic inserts for the
    discriminated trigger union is dropped so the path names only fields the
    caller actually sent. A whole-draft error has no path at all.
    """
    parts: list[str] = []
    for index, segment in enumerate(loc):
        if isinstance(segment, int):
            if parts:
                parts[-1] += f"[{segment}]"
            continue
        if index and loc[index - 1] == "trigger" and segment in _TRIGGER_TAGS:
            continue
        parts.append(segment)
    return ".".join(parts) or None
