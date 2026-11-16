"""Secrets for plugin sources that name a credential with a ``${NAME}`` placeholder.

A plugin can live in a private repository. Its source then carries a
placeholder in the URL user info, for example
``https://x-access-token:${GITHUB_TOKEN}@github.com/acme/plugins.git``. The
source is stored as written; the credential is resolved for each fetch, for
the identity the automation runs as, and handed to the SDK as a
``LookupSecret`` reference:

- local mode (Agent Canvas): the agent server's secret store,
  ``/api/settings/secrets/{name}``;
- cloud mode (SaaS, OHE): short-lived lookup references the OpenHands app
  server signs for the run-as user and org
  (``POST /api/service/users/{user_id}/orgs/{org_id}/secret-lookups``).

A plugin is fetched twice: by this service's own preflight, and by the agent
server (``POST /api/plugins/fetch``). Each gets references it can resolve. The
service never logs or stores a value, and the SDK masks resolved values in its
errors.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from openhands.automation.config import get_config
from openhands.automation.plugin_specs import PluginSecretLookup, PluginSpecError
from openhands.sdk.secret import LookupSecret, SecretValue


SECRET_LOOKUPS_PATH = "/api/service/users/{user_id}/orgs/{org_id}/secret-lookups"
LOCAL_SECRET_PATH = "/api/settings/secrets/{name}"
UNSUPPORTED_MESSAGE = (
    "Private plugin sources are not supported on this deployment yet: the "
    "OpenHands server cannot resolve secrets for plugin sources"
)


@dataclass(frozen=True)
class PluginSourceSecrets:
    """References for some secret names, for each place a plugin is fetched."""

    # Resolved by this service, for its own preflight.
    preflight: dict[str, LookupSecret] = field(default_factory=dict)
    # Sent with ``POST /api/plugins/fetch``; resolved by the agent server.
    fetch: dict[str, LookupSecret] = field(default_factory=dict)

    def fetch_request_secrets(self, names: Collection[str]) -> dict[str, Any]:
        """The ``secrets`` field of an agent-server fetch for ``names``.

        Headers are sent in plain text: the agent server needs them to resolve
        the reference. The payload is never logged or stored.
        """
        return {
            name: self.fetch[name].model_dump(
                mode="json", context={"expose_secrets": "plaintext"}
            )
            for name in names
            if name in self.fetch
        }


def local_plugin_source_secrets(
    names: Collection[str],
    *,
    agent_server_url: str,
    api_key: str,
    sandbox_agent_server_url: str | None = None,
) -> PluginSourceSecrets:
    """References to secrets in the local agent server's store.

    The agent server resolves its own reference against the URL it serves
    itself, which differs from the service's in split setups
    (``sandbox_agent_server_url``).
    """
    headers = {"X-Session-API-Key": api_key} if api_key else {}
    service_base = agent_server_url.rstrip("/")
    fetch_base = (sandbox_agent_server_url or agent_server_url).rstrip("/")
    return PluginSourceSecrets(
        preflight={
            name: LookupSecret(
                url=service_base + LOCAL_SECRET_PATH.format(name=name),
                headers=headers,
            )
            for name in names
        },
        fetch={
            name: LookupSecret(
                url=fetch_base + LOCAL_SECRET_PATH.format(name=name),
                headers=headers,
            )
            for name in names
        },
    )


def _route_missing(response: httpx.Response) -> bool:
    """Whether the app server answered for a route it does not have."""
    if response.status_code == 405:
        return True
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        return True
    return detail == "Not Found"


def _names(names: Collection[str]) -> str:
    return ", ".join(sorted(names))


async def cloud_plugin_source_secrets(
    client: httpx.AsyncClient,
    names: Collection[str],
    *,
    user_id: uuid.UUID,
    org_id: uuid.UUID,
    automation_id: uuid.UUID | None = None,
) -> PluginSourceSecrets:
    """Lookup references the app server signs for ``user_id`` in ``org_id``.

    Raises:
        PluginSpecError: ``missing_secret`` (422) for names the identity does
            not have; ``private_source_unsupported`` (422) when the app server
            has no secret lookup route; ``secret_lookup_failed`` for a
            permanent refusal, with its status; ``secret_unavailable`` with a
            retryable status for an outage.
    """
    config = get_config()
    settings = config.service
    base_url = settings.openhands_api_base_url.rstrip("/")
    body: dict[str, Any] = {"names": sorted(names), "purpose": "plugin_source"}
    if automation_id is not None:
        body["automation_id"] = str(automation_id)
    try:
        response = await client.post(
            base_url + SECRET_LOOKUPS_PATH.format(user_id=user_id, org_id=org_id),
            json=body,
            headers={"X-Service-API-Key": settings.service_key},
            timeout=config.http.http_timeout,
        )
    except httpx.RequestError as exc:
        raise PluginSpecError(
            f"Could not reach the OpenHands API to resolve secret(s) "
            f"{_names(names)} for the plugin source: {type(exc).__name__}",
            code="secret_unavailable",
            status_code=503,
        ) from exc

    status_code = response.status_code
    if status_code in (404, 405) and _route_missing(response):
        raise PluginSpecError(
            UNSUPPORTED_MESSAGE, code="private_source_unsupported", status_code=422
        )
    if status_code in (408, 429) or status_code >= 500:
        raise PluginSpecError(
            f"The OpenHands API could not resolve secret(s) {_names(names)} for "
            f"the plugin source (HTTP {status_code})",
            code="secret_unavailable",
            status_code=status_code,
        )
    if status_code == 404:
        raise PluginSpecError(
            f"Could not resolve secret(s) {_names(names)} for the plugin source "
            "(HTTP 404): the user or service account the automation runs as is "
            "not an active member of the organization",
            code="secret_lookup_failed",
            status_code=404,
        )
    if status_code != 200:
        raise PluginSpecError(
            f"The OpenHands API refused to resolve secret(s) {_names(names)} for "
            f"the plugin source (HTTP {status_code})",
            code="secret_lookup_failed",
            status_code=status_code,
        )

    try:
        data = response.json()
        missing = list(data.get("missing") or [])
        lookup_url = data["lookup_url"]
        lookup_path = data["lookup_path"]
        references = data["secrets"]
        preflight = {
            name: LookupSecret(
                url=base_url + lookup_path, headers=references[name]["headers"]
            )
            for name in names
            if name in references
        }
        fetch = {
            name: LookupSecret(url=lookup_url, headers=references[name]["headers"])
            for name in names
            if name in references
        }
    except (ValueError, KeyError, TypeError) as exc:
        raise PluginSpecError(
            "Unexpected response from the OpenHands API secret lookup",
            code="secret_unavailable",
            status_code=502,
        ) from exc
    missing += [name for name in names if name not in preflight and name not in missing]
    if missing:
        raise PluginSpecError(
            f"Missing secret: {_names(missing)}. Add it under Settings > Secrets "
            "for the user or service account the automation runs as, or connect "
            "the git provider it names.",
            code="missing_secret",
            status_code=422,
        )
    return PluginSourceSecrets(preflight=preflight, fetch=fetch)


async def plugin_source_secrets(
    client: httpx.AsyncClient,
    names: Collection[str],
    *,
    user_id: uuid.UUID,
    org_id: uuid.UUID,
    automation_id: uuid.UUID | None = None,
) -> PluginSourceSecrets:
    """References for ``names``, for the identity an automation runs as."""
    if not names:
        return PluginSourceSecrets()
    settings = get_config().service
    if settings.is_local_mode:
        return local_plugin_source_secrets(
            names,
            agent_server_url=settings.agent_server_url,
            api_key=settings.agent_server_api_key,
            sandbox_agent_server_url=settings.sandbox_agent_server_url or None,
        )
    return await cloud_plugin_source_secrets(
        client, names, user_id=user_id, org_id=org_id, automation_id=automation_id
    )


def preflight_secret_lookup(
    client: httpx.AsyncClient,
    *,
    user_id: uuid.UUID,
    org_id: uuid.UUID,
    automation_id: uuid.UUID | None = None,
) -> PluginSecretLookup:
    """A lookup for this service's preflight, for one run-as identity."""

    async def lookup(names: Collection[str]) -> Mapping[str, SecretValue]:
        if not names:
            return {}
        secrets = await plugin_source_secrets(
            client,
            names,
            user_id=user_id,
            org_id=org_id,
            automation_id=automation_id,
        )
        return secrets.preflight

    return lookup
