"""Who an automation runs as: resolve the identity on create, check a service
account on create and on a run-as change, and hand an upload to it."""

import uuid
from dataclasses import dataclass

import httpx
from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.auth import AuthenticatedUser, require_permission
from openhands.automation.models import Automation, AutomationRunAsKind
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
class ResolvedRunAs:
    """The ``user_id`` and ``run_as_kind`` a new automation is stored with."""

    user_id: uuid.UUID
    kind: AutomationRunAsKind


async def resolve_run_as(
    run_as_user_id: uuid.UUID | None,
    user: AuthenticatedUser,
    client: httpx.AsyncClient,
) -> ResolvedRunAs:
    """Resolve the identity requested by ``run_as_user_id`` (omitted = the caller).

    When omitted, the automation runs as the caller.

    A service account caller is refused (see ``refuse_service_account``).

    Any other identity must be an active service account of the caller's org,
    and only holders of ``manage_automations`` may pick one. The enterprise API
    is the source of truth for the account type; local mode has no service
    accounts, so a requested identity is rejected there.

    Raises:
        HTTPException: 403 for a service account caller, or without
            ``manage_automations``; 422 when the identity is not an active
            service account of the org; 502 when enterprise cannot be asked.
    """
    refuse_service_account(user)
    if run_as_user_id is None:
        return ResolvedRunAs(user.user_id, AutomationRunAsKind.USER)

    require_run_as_permission(
        user, "Only admins and owners can create an automation for a service account"
    )
    await verify_service_account(run_as_user_id, user.org_id, client)
    return ResolvedRunAs(run_as_user_id, AutomationRunAsKind.SERVICE)


def refuse_service_account(
    user: AuthenticatedUser,
    detail: str = "A service account cannot create automations",
) -> None:
    """Refuse a service account caller: 403 with ``detail``.

    Its key is a run's key, and every automation needs a human creator.
    Without this, a run could start new automations that run as itself, or
    make an existing one run as itself. Its role (from the enterprise
    ``/users/me`` response) identifies it.
    """
    if user.role == SERVICE_ACCOUNT_ROLE:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=detail)


_require_view_automations = require_permission("view_automations")


async def require_automation_creator(
    user: AuthenticatedUser = Depends(_require_view_automations),
) -> AuthenticatedUser:
    """Dependency for every route that creates an automation.

    It runs before the handler, so a service account is refused before the
    template replay or draft validation can answer instead.
    """
    refuse_service_account(user)
    return user


def require_run_as_permission(user: AuthenticatedUser, detail: str) -> None:
    """Only holders of ``manage_automations`` may make an automation run as a
    service account, or change who it runs as.

    Raises:
        HTTPException: 403 with ``detail`` without the permission.
    """
    if "manage_automations" not in user.permissions:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=detail)


async def verify_service_account(
    service_account_id: uuid.UUID,
    org_id: uuid.UUID,
    client: httpx.AsyncClient,
    field: str = "run_as_user_id",
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


async def assign_upload_to_run_as(
    session: AsyncSession,
    tarball_path: str,
    org_id: uuid.UUID,
    run_as: ResolvedRunAs,
    *,
    automation_id: uuid.UUID | None = None,
) -> None:
    """Move the internal upload behind ``tarball_path`` to ``run_as``.

    The caller's ownership of the upload is checked first (by
    ``validate_tarball_path``); afterwards the upload belongs to the service
    account, so the caller can no longer delete it from under the automation.
    A path that is not an internal upload is left alone.

    ``automation_id`` is the automation the upload moves with (None for one not
    yet added to the session). An upload is not tied to one automation; moving
    one that another live automation of the org uses would take it from them.

    Raises:
        HTTPException: 409 when another live automation uses the upload.
    """
    upload_id = parse_internal_upload_id(tarball_path)
    if upload_id is None:
        return
    upload = await get_org_upload(session, upload_id, org_id)
    if upload is None or upload.user_id == run_as.user_id:
        return
    shared = select(Automation.id).where(
        Automation.org_id == org_id,
        Automation.tarball_path == tarball_path,
        Automation.deleted_at.is_(None),
    )
    if automation_id is not None:
        shared = shared.where(Automation.id != automation_id)
    if await session.scalar(shared.limit(1)) is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="The upload is used by another automation; upload the code "
            "again for this one",
        )
    upload.user_id = run_as.user_id
