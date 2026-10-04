"""FastAPI router for the git sync status/config/trigger API.

Every endpoint acts on the caller's organization (`user.org_id`): git sync is
org-scoped, so an admin sees and changes only their own org's repo and state.
"""

import asyncio
import logging
import uuid
from typing import Final

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from openhands.automation.auth import AuthenticatedUser, require_permission
from openhands.automation.config import GitSyncSettings, ServiceSettings, get_config
from openhands.automation.db import get_session
from openhands.automation.git_sync.client import GitSyncError, check_remote_access
from openhands.automation.git_sync.config_override import (
    apply_git_sync_config_override,
    find_org_using_repo,
    get_org_config,
    lock_repo_identity,
    resolve_candidate_git_sync_settings,
    resolve_effective_git_sync_settings,
    resolve_effective_sync_interval_seconds,
)
from openhands.automation.git_sync.loop import lease_is_live, run_sync_cycle
from openhands.automation.git_sync.schemas import (
    GitSyncCheckResponse,
    GitSyncConfigUpdateRequest,
    GitSyncStatusResponse,
    GitSyncTriggerResponse,
)
from openhands.automation.git_sync.secret_store import GitSyncSecretStoreError
from openhands.automation.models import AutomationGitSyncState


logger = logging.getLogger("automation.git_sync")

router = APIRouter(prefix="/v1/git-sync", tags=["Git Sync"])

_require_view_automations = require_permission("view_automations")
_require_manage_automations = require_permission("manage_automations")

# Strong references to in-flight manual-trigger tasks: asyncio holds only weak
# ones, so a task could otherwise be GC'd mid-run.
_background_sync_tasks: Final[set[asyncio.Task[None]]] = set()

# Tighter than the sync cycle's per-command timeout, because an operator waits
# on a form for it and an unroutable host blocks the full duration.
_CHECK_TIMEOUT_SECONDS: Final[float] = 20.0

# Maps the config-update request's short field names to GitSyncSettings attrs.
_CONFIG_OVERRIDE_FIELDS: Final[dict[str, str]] = {
    "enabled": "git_sync_enabled",
    "interval_seconds": "git_sync_interval_seconds",
    "repo_url": "git_sync_repo_url",
    "branch": "git_sync_branch",
    "path": "git_sync_path",
    "token": "git_sync_token",
    "encryption_key": "git_sync_encryption_key",
    "author_name": "git_sync_author_name",
    "author_email": "git_sync_author_email",
}

# The settings that say which repo an org syncs to; changing one of them is
# what can collide with another org.
_REPO_IDENTITY_FIELDS: Final[frozenset[str]] = frozenset(
    {"git_sync_repo_url", "git_sync_branch", "git_sync_path"}
)


async def _build_status_response(
    session: AsyncSession, org_id: uuid.UUID, git_settings: GitSyncSettings
) -> GitSyncStatusResponse:
    org_config = await get_org_config(session, org_id)
    dirty_count = await session.scalar(
        select(func.count())
        .select_from(AutomationGitSyncState)
        .where(
            AutomationGitSyncState.org_id == org_id,
            AutomationGitSyncState.dirty.is_(True),
        )
    )

    sync_started_at = org_config.sync_started_at if org_config is not None else None
    sync_in_progress = lease_is_live(sync_started_at, git_settings)

    return GitSyncStatusResponse(
        enabled=git_settings.enabled,
        repo_url=git_settings.git_sync_repo_url,
        branch=git_settings.git_sync_branch,
        path=git_settings.git_sync_path,
        encryption_enabled=bool(git_settings.git_sync_encryption_key),
        interval_seconds=await resolve_effective_sync_interval_seconds(session, org_id),
        last_synced_commit=(
            org_config.last_synced_commit if org_config is not None else None
        ),
        last_synced_at=org_config.last_run_at if org_config is not None else None,
        last_error=(org_config.last_error or None) if org_config is not None else None,
        last_error_at=org_config.last_error_at if org_config is not None else None,
        dirty_count=dirty_count or 0,
        sync_in_progress=sync_in_progress,
        sync_started_at=sync_started_at if sync_in_progress else None,
    )


@router.get("/status")
async def get_git_sync_status(
    user: AuthenticatedUser = Depends(_require_view_automations),
    session: AsyncSession = Depends(get_session),
) -> GitSyncStatusResponse:
    """Report the caller's org's git sync configuration and last-sync state."""
    git_settings = await resolve_effective_git_sync_settings(session, user.org_id)
    return await _build_status_response(session, user.org_id, git_settings)


@router.put("/config")
async def update_git_sync_config(
    data: GitSyncConfigUpdateRequest,
    user: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session),
) -> GitSyncStatusResponse:
    """Configure, reconfigure or pause/resume the org's sync without a restart.

    Configuring a repo is what turns sync on, so everything here takes effect
    immediately -- there is no boot-time flag left to disagree with. The
    caller is recorded as the org's configuring user: automations imported
    from git are created as them (see loop.py).

    Refused with a 409 when another org already syncs the same repository,
    branch and path. Each org's export writes `{path}/{slug}/` and its import
    reads every directory there, so two orgs sharing them would import each
    other's automations.
    """
    update = data.model_dump(exclude_unset=True)
    mapped = {_CONFIG_OVERRIDE_FIELDS[key]: value for key, value in update.items()}

    if _REPO_IDENTITY_FIELDS & mapped.keys():
        candidate = await resolve_candidate_git_sync_settings(
            session, user.org_id, mapped
        )
        if candidate.git_sync_repo_url:
            # Held until this request's session commits, so a concurrent save
            # of the same repo by another org waits and then sees this one.
            await lock_repo_identity(session, candidate)
        if candidate.git_sync_repo_url and (
            await find_org_using_repo(session, candidate, exclude_org_id=user.org_id)
            is not None
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Another organization already syncs this repository, branch "
                    "and path. Sharing them would import each other's "
                    "automations; use a different repository or path."
                ),
            )

    try:
        await apply_git_sync_config_override(
            session, user.org_id, mapped, configured_by_user_id=user.user_id
        )
    except GitSyncSecretStoreError as e:
        # Refusing the write is the point: storing the token unencrypted would
        # silently downgrade exactly what this protects.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Cannot store the git sync secrets securely: {e}",
        ) from e
    await session.commit()

    git_settings = await resolve_effective_git_sync_settings(session, user.org_id)
    return await _build_status_response(session, user.org_id, git_settings)


@router.post("/check")
async def check_git_sync_config(
    data: GitSyncConfigUpdateRequest,
    user: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session),
) -> GitSyncCheckResponse:
    """Test whether a configuration can reach its repo, without saving it.

    Takes `PUT /config`'s body and answers for the settings it *would* leave in
    place for the caller's org. Nothing is persisted and no sync runs -- see
    `check_remote_access`.

    Not gated on sync being enabled: the point is to get the repo URL and token
    right before turning it on. Reaching a caller-chosen URL is no new
    capability, since `PUT /config` already sets it under the same permission.
    """
    candidate = await resolve_candidate_git_sync_settings(
        session,
        user.org_id,
        {
            _CONFIG_OVERRIDE_FIELDS[key]: value
            for key, value in data.model_dump(exclude_unset=True).items()
        },
    )

    if not candidate.git_sync_repo_url:
        return GitSyncCheckResponse(ok=False, detail="No repository URL is configured.")

    try:
        branch_exists = await check_remote_access(
            candidate.git_sync_repo_url,
            candidate.git_sync_branch,
            candidate.git_sync_token,
            min(candidate.git_sync_git_timeout_seconds, _CHECK_TIMEOUT_SECONDS),
        )
    except GitSyncError as e:
        # A failed check is a successful answer about the configuration, not a
        # failed request -- 200 with `ok: false`.
        logger.info("Git sync configuration check failed: %s", e)
        return GitSyncCheckResponse(ok=False, detail=str(e))

    return GitSyncCheckResponse(ok=True, branch_exists=branch_exists)


async def _run_sync_cycle_background(
    session_factory: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    git_settings: GitSyncSettings,
    service_settings: ServiceSettings,
) -> None:
    try:
        result = await run_sync_cycle(
            session_factory, org_id, git_settings, service_settings
        )
    except Exception:
        logger.exception("Manually triggered git sync cycle failed for org %s", org_id)
        return
    if result.skipped:
        logger.info(
            "Manually triggered git sync for org %s skipped: a cycle was "
            "already running",
            org_id,
        )


@router.post("/sync", status_code=status.HTTP_202_ACCEPTED)
async def trigger_git_sync(
    request: Request,
    user: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session),
) -> GitSyncTriggerResponse:
    """Trigger a sync cycle for the caller's org now, instead of at the next poll.

    Fire-and-forget: returns once the cycle is scheduled, not once it completes
    (the same pattern as sandbox cleanup in router.py).
    """
    config = get_config()
    git_settings = await resolve_effective_git_sync_settings(session, user.org_id)
    if not git_settings.enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Git sync is not enabled",
        )

    # A running cycle picks up everything this one would have, and it holds
    # the org's lease anyway, so scheduling another would only be skipped.
    org_config = await get_org_config(session, user.org_id)
    if org_config is not None and lease_is_live(
        org_config.sync_started_at, git_settings
    ):
        return GitSyncTriggerResponse(triggered=False)

    session_factory: async_sessionmaker[AsyncSession] = (
        request.app.state.session_factory
    )
    task = asyncio.create_task(
        _run_sync_cycle_background(
            session_factory, user.org_id, git_settings, config.service
        )
    )
    _background_sync_tasks.add(task)
    task.add_done_callback(_background_sync_tasks.discard)
    return GitSyncTriggerResponse(triggered=True)
