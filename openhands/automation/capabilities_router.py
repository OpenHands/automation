"""Capability discovery and preflight validation for automation setup UIs.

A setup UI needs two answers before it creates anything: what this deployment
supports, so it never offers an option that cannot work, and whether a draft is
acceptable, so a bad configuration is caught before an automation exists.
Neither endpoint writes.
"""

import fnmatch
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Literal
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
    AuthenticatedUser,
    AuthMethod,
    authenticate_request,
    get_http_client,
)
from openhands.automation.config import get_config
from openhands.automation.db import get_session
from openhands.automation.event_schemas import parse_event
from openhands.automation.event_schemas.github import (
    get_supported_event_patterns,
    get_supported_event_types,
)
from openhands.automation.filter_eval import FilterFunctions
from openhands.automation.models import CustomWebhook
from openhands.automation.preset_router import (
    CreatePluginAutomationRequest,
    CreatePromptAutomationRequest,
)
from openhands.automation.scheduler import POLL_INTERVAL_SECONDS
from openhands.automation.schemas import (
    CapabilitiesResponse,
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
from openhands.automation.utils.model_profiles import validate_model_profile_for_user
from openhands.automation.utils.webhook import BUILTIN_SOURCES, get_webhook_config


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["Capabilities"])

DraftModel = CreatePromptAutomationRequest | CreatePluginAutomationRequest

# Draft models keyed by the endpoint they would be posted to. Validating with
# the model creation itself uses is what keeps preflight from drifting.
_DRAFT_MODELS: dict[str, type[DraftModel]] = {
    "/v1/preset/prompt": CreatePromptAutomationRequest,
    "/v1/preset/plugin": CreatePluginAutomationRequest,
}

# Features every deployment has: they come from the SDK code the service
# packages into a run, not from configuration.
_STATIC_FEATURES = (
    "conversationDispatch",
    "mcpTools",
    "presetPlugin",
    "presetPrompt",
    "repoClone",
)

# Tags Pydantic inserts into an error location for the trigger union.
_TRIGGER_TAGS = frozenset({"cron", "event"})

_MCP_PROBE_TIMEOUT_SECONDS = 15.0
_MAX_SECRET_SEARCH_PAGES = 10
_LOCAL_REPOSITORY_SECRET_NAMES = {
    "github": "github_token",
    "gitlab": "gitlab_token",
    "bitbucket": "bitbucket_token",
}


class _DependencyUnavailable(Exception):
    """A trusted validation dependency did not return a usable answer."""


@dataclass(frozen=True)
class _PreflightTarget:
    base_url: str
    headers: dict[str, str]
    local: bool


@dataclass(frozen=True)
class _StoredMCPServer:
    name: str
    raw: dict[str, Any]
    transport: Literal["stdio", "shttp", "sse"]
    locator: str
    auth_strategy: str
    enabled: bool


@router.get("/capabilities", response_model_exclude_none=True)
async def get_capabilities(
    user: AuthenticatedUser = Depends(authenticate_request),
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

    builtin_sources = sorted(BUILTIN_SOURCES) if config.service.webhook_secret else []
    event_sources = sorted(
        {*builtin_sources, *await _custom_sources(user.org_id, session)}
    )

    features = [*_STATIC_FEATURES]
    if event_sources:
        features.append("webhookDelivery")
    if config.kv.enabled:
        features.append("kvStore")

    return CapabilitiesResponse(
        ready=True,
        max_automation_timeout_seconds=config.sandbox.max_run_duration,
        trigger_kinds=["cron", "event"] if event_sources else ["cron"],
        event_sources=event_sources,
        event_types=(
            get_supported_event_patterns() if "github" in builtin_sources else []
        ),
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
    user: AuthenticatedUser = Depends(authenticate_request),
    session: AsyncSession = Depends(get_session),
    client: httpx.AsyncClient = Depends(get_http_client),
) -> ValidateDraftResponse:
    """Validate a draft automation without creating it.

    An invalid draft is still a successful validation: the response is 200 with
    `valid` false and one error per problem, each addressed to the field that
    caused it. Only a malformed request envelope is a 4xx.
    """
    logger.info(
        "Validating draft for %s (automation_id=%s)", body.endpoint, body.automation_id
    )

    try:
        draft = _DRAFT_MODELS[body.endpoint].model_validate(body.draft)
    except ValidationError as e:
        return ValidateDraftResponse(valid=False, errors=_schema_errors(e))

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
        try:
            errors.extend(
                await _deployment_preflight_errors(
                    body=body,
                    draft=draft,
                    user=user,
                    request=request,
                    client=client,
                )
            )
        except _DependencyUnavailable:
            logger.warning("A preflight validation dependency is unavailable")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Preflight validation is temporarily unavailable.",
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
    requirements = body.requirements
    if requirements is None:
        return []

    target = _preflight_target(request, user)
    requested_secret_names = {
        name
        for requirement in requirements.integrations
        for alternative in requirement.alternatives
        for name in alternative.secret_names
    }
    if target.local and draft.repos:
        requested_secret_names.update(_LOCAL_REPOSITORY_SECRET_NAMES.values())

    available_secret_names = await _available_secret_names(
        target,
        requested_secret_names,
        client,
    )
    errors: list[DraftValidationError] = []

    if requirements.integrations:
        servers = await _stored_mcp_servers(target, client)
        for requirement in requirements.integrations:
            errors.extend(
                await _integration_errors(
                    requirement,
                    servers,
                    available_secret_names,
                    target,
                    client,
                )
            )

    if draft.repos:
        for index, repository in enumerate(draft.repos):
            errors.extend(
                await _repository_errors(
                    repository=repository,
                    index=index,
                    requirements=requirements.integrations,
                    available_secret_names=available_secret_names,
                    target=target,
                    client=client,
                )
            )

    return errors


def _preflight_target(request: Request, user: AuthenticatedUser) -> _PreflightTarget:
    settings = get_config().service
    if settings.is_local_mode:
        headers = {}
        if settings.agent_server_api_key:
            headers["X-Session-API-Key"] = settings.agent_server_api_key
        return _PreflightTarget(
            base_url=settings.agent_server_url.rstrip("/"),
            headers=headers,
            local=True,
        )

    if user.auth_method == AuthMethod.API_KEY and user.api_key:
        headers = {"Authorization": f"Bearer {user.api_key}"}
    elif user.auth_method == AuthMethod.COOKIE:
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
        headers = {"Cookie": "; ".join(cookies)}
    else:
        raise _DependencyUnavailable

    return _PreflightTarget(
        base_url=settings.openhands_api_base_url.rstrip("/"),
        headers=headers,
        local=False,
    )


async def _send_preflight_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    **kwargs: Any,
) -> httpx.Response:
    try:
        response = await client.request(method, url, headers=headers, **kwargs)
    except httpx.RequestError:
        raise _DependencyUnavailable from None
    if response.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
        raise _DependencyUnavailable
    if response.status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR:
        raise _DependencyUnavailable
    return response


def _json_object(response: httpx.Response) -> dict[str, Any]:
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
        items = _json_object(response).get("secrets")
        if not isinstance(items, list):
            raise _DependencyUnavailable
        names: set[str] = set()
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                raise _DependencyUnavailable
            names.add(item["name"])
        return names.intersection(requested_names)

    found: set[str] = set()
    for name in sorted(requested_names):
        page_id: str | None = None
        for _ in range(_MAX_SECRET_SEARCH_PAGES):
            params: dict[str, str | int] = {
                "name__contains": name,
                "limit": 100,
            }
            if page_id is not None:
                params["page_id"] = page_id
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
            items = data.get("items")
            if not isinstance(items, list):
                raise _DependencyUnavailable
            item_names: list[str] = []
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                    raise _DependencyUnavailable
                item_names.append(item["name"])
            if name in item_names:
                found.add(name)
                break
            next_page_id = data.get("next_page_id")
            if next_page_id is None:
                break
            if not isinstance(next_page_id, str) or not next_page_id:
                raise _DependencyUnavailable
            page_id = next_page_id
        else:
            raise _DependencyUnavailable
    return found


async def _stored_mcp_servers(
    target: _PreflightTarget,
    client: httpx.AsyncClient,
) -> list[_StoredMCPServer]:
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
            raise _DependencyUnavailable
        servers.append(_parse_stored_mcp_server(name, raw))
    return servers


def _parse_stored_mcp_server(name: str, raw: dict[str, Any]) -> _StoredMCPServer:
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise _DependencyUnavailable

    raw_transport = raw.get("transport", raw.get("type"))
    if raw_transport is None:
        raw_transport = "stdio" if isinstance(raw.get("command"), str) else "http"
    if raw_transport == "stdio":
        transport: Literal["stdio", "shttp", "sse"] = "stdio"
        locator = name
    elif raw_transport in {"http", "streamable-http", "shttp"}:
        transport = "shttp"
        locator = raw.get("url")
    elif raw_transport == "sse":
        transport = "sse"
        locator = raw.get("url")
    else:
        raise _DependencyUnavailable
    if not isinstance(locator, str) or not locator:
        raise _DependencyUnavailable

    raw_auth = raw.get("auth")
    if raw_auth is None:
        auth_strategy = "none"
    elif isinstance(raw_auth, dict) and isinstance(raw_auth.get("strategy"), str):
        auth_strategy = raw_auth["strategy"]
    else:
        raise _DependencyUnavailable
    return _StoredMCPServer(
        name=name,
        raw=raw,
        transport=transport,
        locator=locator,
        auth_strategy=auth_strategy,
        enabled=enabled,
    )


def _normalized_remote_locator(value: str) -> tuple[str, str, int | None, str] | None:
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
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
    )


def _alternative_matches_server(
    alternative: PreflightIntegrationAlternative,
    server: _StoredMCPServer,
) -> bool:
    if alternative.transport != server.transport:
        return False
    if alternative.transport == "stdio":
        if alternative.locator != server.locator:
            return False
    elif _normalized_remote_locator(alternative.locator) != _normalized_remote_locator(
        server.locator
    ):
        return False

    expected_auth = alternative.auth_strategy
    if expected_auth is None:
        return True
    if server.auth_strategy == "header" and expected_auth == "none":
        return True
    return server.auth_strategy == expected_auth


async def _integration_errors(
    requirement: PreflightIntegrationRequirement,
    servers: list[_StoredMCPServer],
    available_secret_names: set[str],
    target: _PreflightTarget,
    client: httpx.AsyncClient,
) -> list[DraftValidationError]:
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
    )
    if response.status_code != status.HTTP_200_OK:
        raise _DependencyUnavailable
    ok = _json_object(response).get("ok")
    if not isinstance(ok, bool):
        raise _DependencyUnavailable
    return ok


def _repository_parts(repository: Any) -> tuple[str, str] | None:
    try:
        provider = repository.get_provider().value
    except ValueError:
        return None

    raw_url = repository.url
    if raw_url.startswith("git@"):
        _, separator, identifier = raw_url.partition(":")
        if not separator:
            return None
    elif "://" in raw_url:
        parsed = urlparse(raw_url)
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
    repository: Any,
    index: int,
    requirements: list[PreflightIntegrationRequirement],
    available_secret_names: set[str],
    target: _PreflightTarget,
    client: httpx.AsyncClient,
) -> list[DraftValidationError]:
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
            ref=repository.ref,
            index=index,
            requirements=requirements,
            available_secret_names=available_secret_names,
            target=target,
            client=client,
        )
    return await _cloud_repository_errors(
        provider=provider,
        identifier=identifier,
        ref=repository.ref,
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
    requirements: list[PreflightIntegrationRequirement],
    available_secret_names: set[str],
    target: _PreflightTarget,
    client: httpx.AsyncClient,
) -> list[DraftValidationError]:
    candidate_names: list[str] = []
    for requirement in requirements:
        if requirement.id != provider:
            continue
        for alternative in requirement.alternatives:
            for name in alternative.secret_names:
                if name in available_secret_names and name not in candidate_names:
                    candidate_names.append(name)
    canonical_name = _LOCAL_REPOSITORY_SECRET_NAMES[provider]
    if (
        canonical_name in available_secret_names
        and canonical_name not in candidate_names
    ):
        candidate_names.append(canonical_name)

    response = await _send_preflight_request(
        client,
        "POST",
        f"{target.base_url}/api/git/validate-repository",
        headers=target.headers,
        json={
            "provider": provider,
            "repository": identifier,
            "ref": ref,
            "credential_names": candidate_names[:5],
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
    response = await _send_preflight_request(
        client,
        "GET",
        f"{target.base_url}/api/v1/git/repositories/search",
        headers=target.headers,
        params={"provider": provider, "query": identifier, "limit": 100},
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
    items = _json_object(response).get("items")
    if not isinstance(items, list):
        raise _DependencyUnavailable
    names: list[str] = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("full_name"), str):
            raise _DependencyUnavailable
        names.append(item["full_name"])
    if identifier.casefold() not in {name.casefold() for name in names}:
        return [
            DraftValidationError(
                field=f"repos[{index}].url",
                code="repository_not_accessible",
                message=f"The repository {identifier} could not be accessed.",
            )
        ]
    if ref is None:
        return []

    response = await _send_preflight_request(
        client,
        "GET",
        f"{target.base_url}/api/v1/git/branches/search",
        headers=target.headers,
        params={
            "provider": provider,
            "repository": identifier,
            "query": ref,
            "limit": 100,
        },
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
    branch_items = _json_object(response).get("items")
    if not isinstance(branch_items, list):
        raise _DependencyUnavailable
    ref_matches = False
    for item in branch_items:
        if not isinstance(item, dict):
            raise _DependencyUnavailable
        name = item.get("name")
        commit_sha = item.get("commit_sha")
        if not isinstance(name, str) or not isinstance(commit_sha, str):
            raise _DependencyUnavailable
        if name == ref or commit_sha.casefold().startswith(ref.casefold()):
            ref_matches = True
            break
    if ref_matches:
        return []
    return [
        DraftValidationError(
            field=f"repos[{index}].ref",
            code="repository_ref_not_accessible",
            message=f"The Git reference '{ref}' could not be accessed.",
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
