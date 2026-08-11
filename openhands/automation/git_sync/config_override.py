"""Runtime overrides for git sync config, on top of the env-var defaults.

Lets `PUT /v1/git-sync/config` reconfigure or pause/resume an already-running
sync without a restart. Overrides are stored as a single JSON blob in the
existing `automation_service_metadata` table -- no new table needed.
"""

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.config import GitSyncSettings
from openhands.automation.utils.service_metadata import (
    get_service_metadata,
    set_service_metadata,
)


GIT_SYNC_CONFIG_OVERRIDE_KEY = "git_sync_config_override"


async def resolve_effective_git_sync_settings(
    session: AsyncSession, git_settings: GitSyncSettings
) -> GitSyncSettings:
    """Merge persisted runtime overrides over the env-var defaults."""
    raw = await get_service_metadata(session, GIT_SYNC_CONFIG_OVERRIDE_KEY)
    if not raw:
        return git_settings
    overrides = json.loads(raw)
    return git_settings.model_copy(update=overrides)


async def apply_git_sync_config_override(
    session: AsyncSession, updates: dict[str, Any]
) -> None:
    """Persist partial overrides, keyed by `GitSyncSettings` field name.

    A `None` value clears that field's override (reverts to the env-var
    default) rather than being stored literally -- the settings fields are
    plain `str`/`bool`/`int`, so a literal `None` would corrupt `model_copy`.
    """
    raw = await get_service_metadata(session, GIT_SYNC_CONFIG_OVERRIDE_KEY)
    overrides = json.loads(raw) if raw else {}
    for key, value in updates.items():
        if value is None:
            overrides.pop(key, None)
        else:
            overrides[key] = value
    await set_service_metadata(
        session, GIT_SYNC_CONFIG_OVERRIDE_KEY, json.dumps(overrides)
    )
