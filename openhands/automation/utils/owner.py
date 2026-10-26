"""Resolve who owns a new automation: the caller, or a service account."""

import uuid
from dataclasses import dataclass

import httpx
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.auth import AuthenticatedUser
from openhands.automation.models import AutomationOwnerKind
from openhands.automation.utils.enterprise_client import (
    EnterpriseClientError,
    get_org_member,
)
from openhands.automation.utils.tarball_validation import (
    get_org_upload,
    parse_internal_upload_id,
)


# The org role enterprise gives every service account.
SERVICE_ACCOUNT_ROLE = "service_account"


@dataclass(frozen=True)
class ResolvedOwner:
    """The ``user_id`` and ``owner_kind`` a new automation is stored with."""

    user_id: uuid.UUID
    kind: AutomationOwnerKind


async def resolve_owner(
    owner_user_id: uuid.UUID | None,
    user: AuthenticatedUser,
    client: httpx.AsyncClient,
) -> ResolvedOwner:
    """Resolve the owner requested by ``owner_user_id`` (omitted = the caller).

    When omitted, the caller owns the automation. If the caller is a service
    account, the row is service-owned.

    Another owner must be an active service account of the caller's org, and
    only holders of ``manage_automations`` may pick one. The enterprise API is
    the source of truth for the account type; local mode has no service
    accounts, so a requested owner is rejected there.

    Raises:
        HTTPException: 403 without ``manage_automations``; 422 when the owner
            is not an active service account of the org; 502 when enterprise
            cannot be asked.
    """
    if owner_user_id is None:
        # A service account's own key must not hold the creator's edit rights
        # over what it creates, or it could change what runs as itself. Its
        # role (from the enterprise ``/users/me`` response) identifies it.
        kind = (
            AutomationOwnerKind.SERVICE
            if user.role == SERVICE_ACCOUNT_ROLE
            else AutomationOwnerKind.USER
        )
        return ResolvedOwner(user.user_id, kind)

    if "manage_automations" not in user.permissions:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="Only admins and owners can create an automation for a "
            "service account",
        )
    await verify_service_account(owner_user_id, user.org_id, client)
    return ResolvedOwner(owner_user_id, AutomationOwnerKind.SERVICE)


async def verify_service_account(
    service_account_id: uuid.UUID,
    org_id: uuid.UUID,
    client: httpx.AsyncClient,
    field: str = "owner_user_id",
) -> None:
    """Check that ``service_account_id`` is an active service account of ``org_id``.

    The enterprise API is the source of truth for the account type. Local mode
    has no service accounts, so every id is rejected there.

    ``field`` names the request field in the 422 message.

    Raises:
        HTTPException: 422 when it is not an active service account of the
            org; 502 when enterprise cannot be asked.
    """
    try:
        member = await get_org_member(client, org_id, service_account_id)
    except EnterpriseClientError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail="Could not verify the service account",
        ) from exc

    if (
        member is None
        or member.org_id != org_id
        or member.account_type != "service"
        or member.status != "active"
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{field} must be an active service account of this organization",
        )


async def assign_upload_to_owner(
    session: AsyncSession, tarball_path: str, org_id: uuid.UUID, owner: ResolvedOwner
) -> None:
    """Move the internal upload behind ``tarball_path`` to ``owner``.

    The caller's ownership of the upload is checked first (by
    ``validate_tarball_path``); afterwards the upload belongs to the service
    account, so the caller can no longer delete it from under the automation.
    A path that is not an internal upload is left alone.
    """
    upload_id = parse_internal_upload_id(tarball_path)
    if upload_id is None:
        return
    upload = await get_org_upload(session, upload_id, org_id)
    if upload is not None:
        upload.user_id = owner.user_id
