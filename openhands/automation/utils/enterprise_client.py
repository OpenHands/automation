"""Client for the enterprise service API (``/api/service/*``).

Calls authenticate with the service key (``X-Service-API-Key``), the same
credential used to mint a run's API key in ``utils/api_key.py``.
"""

import logging
import uuid
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from openhands.automation.config import get_config


logger = logging.getLogger(__name__)

# The one place that knows the membership route. ``{user_id}`` and ``{org_id}``
# are filled in by ``get_org_member``.
ORG_MEMBERSHIP_PATH = "/api/service/users/{user_id}/orgs/{org_id}/membership"


class EnterpriseClientError(Exception):
    """The enterprise service API could not answer (not a "no" answer)."""


class OrgMemberInfo(BaseModel):
    """A user's membership in an org, as the enterprise service API reports it."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    user_id: uuid.UUID
    org_id: uuid.UUID
    account_type: Literal["human", "service"]
    role: str
    # "active" or "disabled" today; kept open so a new value is not a parse error.
    status: str


async def get_org_member(
    client: httpx.AsyncClient, org_id: uuid.UUID, user_id: uuid.UUID
) -> OrgMemberInfo | None:
    """Look up ``user_id``'s membership in ``org_id``.

    Returns ``None`` when the user is not a member (404) and, in local mode,
    always: a local install has no enterprise API and no service accounts.

    Raises:
        EnterpriseClientError: The service key is not configured, the request
            failed, or the answer was not a membership.
    """
    config = get_config().service
    if config.is_local_mode:
        return None
    if not config.service_key:
        raise EnterpriseClientError("Service key is not configured")

    url = config.openhands_api_base_url.rstrip("/") + ORG_MEMBERSHIP_PATH.format(
        user_id=user_id, org_id=org_id
    )
    try:
        response = await client.get(
            url, headers={"X-Service-API-Key": config.service_key}
        )
    except httpx.RequestError as exc:
        raise EnterpriseClientError(f"Membership request failed: {exc}") from exc

    if response.status_code == 404:
        return None
    if response.status_code != 200:
        logger.error(
            "Membership lookup for user %s in org %s returned HTTP %s",
            user_id,
            org_id,
            response.status_code,
        )
        raise EnterpriseClientError(f"HTTP {response.status_code}")
    try:
        return OrgMemberInfo.model_validate(response.json())
    except (ValueError, ValidationError) as exc:
        raise EnterpriseClientError("Unexpected membership response") from exc


# ``{org_id}`` and ``{factory_id}`` are filled in by ``notify_factory_deleted``.
FACTORY_WORKLOGS_PATH = "/api/service/orgs/{org_id}/factories/{factory_id}/worklogs"


async def notify_factory_deleted(
    client: httpx.AsyncClient, org_id: uuid.UUID, factory_id: uuid.UUID
) -> None:
    """Tell Enterprise to purge a deleted factory's worklogs.

    Called after the delete commits. It never raises: a missed notification
    must not undo or block the delete, and Enterprise's maintenance task repairs
    one. Skipped in local mode, which has no Enterprise server.
    """
    config = get_config().service
    if config.is_local_mode:
        return
    if not config.service_key:
        logger.warning(
            "Not notifying Enterprise of deleted factory %s: no service key",
            factory_id,
        )
        return

    url = config.openhands_api_base_url.rstrip("/") + FACTORY_WORKLOGS_PATH.format(
        org_id=org_id, factory_id=factory_id
    )
    try:
        response = await client.delete(
            url, headers={"X-Service-API-Key": config.service_key}
        )
    except httpx.HTTPError as exc:
        logger.warning(
            "Worklog purge for deleted factory %s failed: %s",
            factory_id,
            type(exc).__name__,
        )
        return
    if response.status_code != 204:
        logger.warning(
            "Worklog purge for deleted factory %s returned HTTP %s",
            factory_id,
            response.status_code,
        )
