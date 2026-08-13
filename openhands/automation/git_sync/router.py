"""FastAPI router for the git sync status/config/trigger API."""

import asyncio
import logging
from datetime import datetime
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
    resolve_candidate_git_sync_settings,
    resolve_effective_git_sync_settings,
    resolve_effective_sync_interval_seconds,
)
from openhands.automation.git_sync.loop import (
    GIT_SYNC_LAST_COMMIT_KEY,
    GIT_SYNC_LAST_ERROR_AT_KEY,
    GIT_SYNC_LAST_ERROR_KEY,
    GIT_SYNC_LAST_RUN_AT_KEY,
    get_sync_started_at,
    is_git_sync_opted_in,
    run_sync_cycle,
)
from openhands.automation.git_sync.schemas import (
    GitSyncCheckResponse,
    GitSyncConfigUpdateRequest,
    GitSyncStatusResponse,
    GitSyncTriggerResponse,
)
from openhands.automation.git_sync.secret_store import GitSyncSecretStoreError
from openhands.automation.models import AutomationGitSyncState
from openhands.automation.utils.service_metadata import get_service_metadata


logger = logging.getLogger("automation.git_sync")

router = APIRouter(prefix="/v1/git-sync", tags=["Git Sync"])

_require_manage_automations = require_permission("manage_automations")

# Strong references to in-flight manual-trigger tasks -- asyncio only holds
# weak references to tasks, so without this one could be GC'd mid-run.
_background_sync_tasks: Final[set[asyncio.Task]] = set()

# A reachability check runs while an operator waits on a form, so it gets a
# tighter bound than the sync cycle's per-command timeout: an unroutable host
# blocks for the full duration before git gives up.
_CHECK_TIMEOUT_SECONDS: Final = 20.0

# Maps the config-update request's short field names to GitSyncSettings attrs.
_CONFIG_OVERRIDE_FIELDS: Final = {
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


def _is_effectively_enabled(git_settings: GitSyncSettings) -> bool:
    """Whether sync is on right now: the boot-time opt-in plus live config.

    `is_git_sync_opted_in()` is what keeps `PUT /config` from newly enabling
    sync in a deployment that booted with it off. Without it, this gate read
    only the override-merged `enabled`, so any caller with
    `manage_automations` could turn sync on and point it at a repo of their
    choosing -- `POST /sync` would then push every automation's prompt,
    model config and tarball contents there.
    """
    return is_git_sync_opted_in() and git_settings.enabled


async def _build_status_response(
    session: AsyncSession, git_settings: GitSyncSettings
) -> GitSyncStatusResponse:
    last_commit = await get_service_metadata(session, GIT_SYNC_LAST_COMMIT_KEY)
    last_run_at = await get_service_metadata(session, GIT_SYNC_LAST_RUN_AT_KEY)
    last_error = await get_service_metadata(session, GIT_SYNC_LAST_ERROR_KEY)
    last_error_at = await get_service_metadata(session, GIT_SYNC_LAST_ERROR_AT_KEY)
    dirty_count = await session.scalar(
        select(func.count())
        .select_from(AutomationGitSyncState)
        .where(AutomationGitSyncState.dirty.is_(True))
    )

    sync_started_at = get_sync_started_at()

    return GitSyncStatusResponse(
        enabled=_is_effectively_enabled(git_settings),
        repo_url=git_settings.git_sync_repo_url,
        branch=git_settings.git_sync_branch,
        path=git_settings.git_sync_path,
        encryption_enabled=bool(git_settings.git_sync_encryption_key),
        interval_seconds=await resolve_effective_sync_interval_seconds(session),
        last_synced_commit=last_commit,
        last_synced_at=datetime.fromisoformat(last_run_at) if last_run_at else None,
        last_error=last_error or None,
        last_error_at=datetime.fromisoformat(last_error_at) if last_error_at else None,
        dirty_count=dirty_count or 0,
        sync_in_progress=sync_started_at is not None,
        sync_started_at=sync_started_at,
    )


@router.get("/status")
async def get_git_sync_status(
    _user: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session),
) -> GitSyncStatusResponse:
    """Report git sync configuration and last-sync state."""
    git_settings = await resolve_effective_git_sync_settings(
        session, get_config().git_sync
    )
    return await _build_status_response(session, git_settings)


@router.put("/config")
async def update_git_sync_config(
    data: GitSyncConfigUpdateRequest,
    _user: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session),
) -> GitSyncStatusResponse:
    """Reconfigure or pause/resume an already-running sync without a restart.

    Can't newly enable sync in a deployment that booted with it disabled --
    that still requires AUTOMATION_GIT_SYNC_ENABLED plus a restart.
    """
    if data.enabled and not is_git_sync_opted_in():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Git sync can only be enabled in a deployment that booted "
                "with AUTOMATION_GIT_SYNC_ENABLED set and is running in "
                "local mode. Set it and restart the service."
            ),
        )

    update = data.model_dump(exclude_unset=True)
    mapped = {_CONFIG_OVERRIDE_FIELDS[key]: value for key, value in update.items()}
    try:
        await apply_git_sync_config_override(session, mapped)
    except GitSyncSecretStoreError as e:
        # Refusing the write is the point: storing the token unencrypted
        # instead would be a silent downgrade of exactly what this protects.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Cannot store the git sync secrets securely: {e}",
        ) from e
    await session.commit()

    git_settings = await resolve_effective_git_sync_settings(
        session, get_config().git_sync
    )
    return await _build_status_response(session, git_settings)


@router.post("/check")
async def check_git_sync_config(
    data: GitSyncConfigUpdateRequest,
    _user: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session),
) -> GitSyncCheckResponse:
    """Test whether a configuration can reach its repo, without saving it.

    Takes the same body as `PUT /config` and answers for the settings that
    body *would* leave in place, so a form can be checked before it is
    stored. Nothing is persisted and no sync runs -- see
    `check_remote_access` for why this must not be a sync cycle.

    Not gated on git sync being enabled: the point is to get a repo URL and
    token right before turning it on. Reaching a URL of the caller's choosing
    is no new capability either -- `PUT /config` already sets the URL the
    sync loop connects to, behind the same `manage_automations` permission.
    """
    candidate = await resolve_candidate_git_sync_settings(
        session,
        get_config().git_sync,
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
        # A failed check is a successful answer about the configuration, not
        # a failure of this request -- 200 with `ok: false`.
        logger.info("Git sync configuration check failed: %s", e)
        return GitSyncCheckResponse(ok=False, detail=str(e))

    return GitSyncCheckResponse(ok=True, branch_exists=branch_exists)


async def _run_sync_cycle_background(
    session_factory: async_sessionmaker[AsyncSession],
    git_settings: GitSyncSettings,
    service_settings: ServiceSettings,
) -> None:
    try:
        await run_sync_cycle(session_factory, git_settings, service_settings)
    except Exception:
        logger.exception("Manually triggered git sync cycle failed")


@router.post("/sync", status_code=status.HTTP_202_ACCEPTED)
async def trigger_git_sync(
    request: Request,
    _user: AuthenticatedUser = Depends(_require_manage_automations),
    session: AsyncSession = Depends(get_session),
) -> GitSyncTriggerResponse:
    """Trigger a sync cycle immediately instead of waiting for the next poll.

    Fire-and-forget: returns as soon as the cycle is scheduled, not once it
    completes (the same pattern as sandbox cleanup in router.py).
    """
    config = get_config()
    git_settings = await resolve_effective_git_sync_settings(session, config.git_sync)
    if not _is_effectively_enabled(git_settings):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Git sync is not enabled",
        )

    # A cycle already running picks up everything this one would have -- and
    # `run_sync_cycle` serializes on a lock anyway, so scheduling another only
    # queues a redundant round trip behind it.
    if get_sync_started_at() is not None:
        return GitSyncTriggerResponse(triggered=False)

    session_factory: async_sessionmaker[AsyncSession] = (
        request.app.state.session_factory
    )
    task = asyncio.create_task(
        _run_sync_cycle_background(session_factory, git_settings, config.service)
    )
    _background_sync_tasks.add(task)
    task.add_done_callback(_background_sync_tasks.discard)
    return GitSyncTriggerResponse(triggered=True)
