"""FastAPI router for the git sync status/config/trigger API."""

import asyncio
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from openhands.automation.auth import AuthenticatedUser, require_permission
from openhands.automation.config import GitSyncSettings, ServiceSettings, get_config
from openhands.automation.db import get_session
from openhands.automation.git_sync.config_override import (
    apply_git_sync_config_override,
    resolve_effective_git_sync_settings,
)
from openhands.automation.git_sync.loop import (
    GIT_SYNC_LAST_COMMIT_KEY,
    GIT_SYNC_LAST_ERROR_AT_KEY,
    GIT_SYNC_LAST_ERROR_KEY,
    GIT_SYNC_LAST_RUN_AT_KEY,
    run_sync_cycle,
)
from openhands.automation.git_sync.schemas import (
    GitSyncConfigUpdateRequest,
    GitSyncStatusResponse,
    GitSyncTriggerResponse,
)
from openhands.automation.models import AutomationGitSyncState
from openhands.automation.utils.service_metadata import get_service_metadata


logger = logging.getLogger("automation.git_sync")

router = APIRouter(prefix="/v1/git-sync", tags=["Git Sync"])

_require_manage_automations = require_permission("manage_automations")

# Strong references to in-flight manual-trigger tasks -- asyncio only holds
# weak references to tasks, so without this one could be GC'd mid-run.
_background_sync_tasks: set[asyncio.Task] = set()

# Maps the config-update request's short field names to GitSyncSettings attrs.
_CONFIG_OVERRIDE_FIELDS = {
    "enabled": "git_sync_enabled",
    "repo_url": "git_sync_repo_url",
    "branch": "git_sync_branch",
    "path": "git_sync_path",
    "token": "git_sync_token",
    "encryption_key": "git_sync_encryption_key",
    "author_name": "git_sync_author_name",
    "author_email": "git_sync_author_email",
}


def _is_effectively_enabled(git_settings: GitSyncSettings) -> bool:
    return get_config().service.is_local_mode and git_settings.enabled


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

    return GitSyncStatusResponse(
        enabled=_is_effectively_enabled(git_settings),
        repo_url=git_settings.git_sync_repo_url,
        branch=git_settings.git_sync_branch,
        path=git_settings.git_sync_path,
        encryption_enabled=bool(git_settings.git_sync_encryption_key),
        last_synced_commit=last_commit,
        last_synced_at=datetime.fromisoformat(last_run_at) if last_run_at else None,
        last_error=last_error or None,
        last_error_at=datetime.fromisoformat(last_error_at) if last_error_at else None,
        dirty_count=dirty_count or 0,
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
    update = data.model_dump(exclude_unset=True)
    mapped = {_CONFIG_OVERRIDE_FIELDS[key]: value for key, value in update.items()}
    await apply_git_sync_config_override(session, mapped)
    await session.commit()

    git_settings = await resolve_effective_git_sync_settings(
        session, get_config().git_sync
    )
    return await _build_status_response(session, git_settings)


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

    session_factory: async_sessionmaker[AsyncSession] = (
        request.app.state.session_factory
    )
    task = asyncio.create_task(
        _run_sync_cycle_background(session_factory, git_settings, config.service)
    )
    _background_sync_tasks.add(task)
    task.add_done_callback(_background_sync_tasks.discard)
    return GitSyncTriggerResponse(triggered=True)
