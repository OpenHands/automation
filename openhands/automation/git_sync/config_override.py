"""Runtime overrides for git sync config, on top of the env-var defaults.

Lets `PUT /v1/git-sync/config` reconfigure or pause/resume a running sync
without a restart. Stored as one JSON blob in `automation_service_metadata`.
"""

import json
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.config import GitSyncSettings
from openhands.automation.git_sync.secret_store import (
    decrypt_secret_fields,
    encrypt_secret_fields,
)
from openhands.automation.utils.service_metadata import (
    get_service_metadata,
    set_service_metadata,
)


GIT_SYNC_CONFIG_OVERRIDE_KEY: Final[str] = "git_sync_config_override"

# Runtime-only config: no env var, set solely from the UI. It rides in the same
# blob but is not a GitSyncSettings field, so it is filtered out before merging.
SYNC_INTERVAL_OVERRIDE_KEY: Final[str] = "git_sync_interval_seconds"

# 0 means manual-only: nothing syncs until POST /v1/git-sync/sync is called.
DEFAULT_SYNC_INTERVAL_SECONDS: Final[int] = 0


async def _load_overrides(session: AsyncSession) -> dict[str, Any]:
    raw = await get_service_metadata(session, GIT_SYNC_CONFIG_OVERRIDE_KEY)
    return decrypt_secret_fields(json.loads(raw)) if raw else {}


async def _store_overrides(session: AsyncSession, overrides: dict[str, Any]) -> None:
    await set_service_metadata(
        session,
        GIT_SYNC_CONFIG_OVERRIDE_KEY,
        json.dumps(encrypt_secret_fields(overrides)),
    )


async def resolve_effective_git_sync_settings(
    session: AsyncSession, git_settings: GitSyncSettings
) -> GitSyncSettings:
    """Merge persisted runtime overrides over the env-var defaults."""
    overrides = {
        key: value
        for key, value in (await _load_overrides(session)).items()
        if key != SYNC_INTERVAL_OVERRIDE_KEY
    }
    if not overrides:
        return git_settings
    return git_settings.model_copy(update=overrides)


async def resolve_candidate_git_sync_settings(
    session: AsyncSession, git_settings: GitSyncSettings, updates: dict[str, Any]
) -> GitSyncSettings:
    """The settings a `PUT /config` with `updates` would leave in place.

    The same merge as apply + resolve, but persisting nothing: `POST /check`
    tests a configuration before it is saved. Handles the None-clears-the-
    override case, where the effective value is the env default, not a blank.
    """
    overrides = {
        key: value
        for key, value in (await _load_overrides(session)).items()
        if key != SYNC_INTERVAL_OVERRIDE_KEY
    }
    for key, value in updates.items():
        # Dropped here as it is on the way out of storage: not a settings
        # field, and `model_copy` would graft it onto the model unvalidated.
        if key == SYNC_INTERVAL_OVERRIDE_KEY:
            continue
        if value is None:
            overrides.pop(key, None)
        else:
            overrides[key] = value
    return git_settings.model_copy(update=overrides) if overrides else git_settings


async def resolve_effective_sync_interval_seconds(session: AsyncSession) -> int:
    """Seconds between automatic syncs; 0 means manual-only.

    A stored value that isn't a non-negative int is ignored rather than left to
    crash or busy-spin the sync loop.
    """
    value = (await _load_overrides(session)).get(SYNC_INTERVAL_OVERRIDE_KEY)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return DEFAULT_SYNC_INTERVAL_SECONDS
    return value


async def apply_git_sync_config_override(
    session: AsyncSession, updates: dict[str, Any]
) -> None:
    """Persist partial overrides, keyed by `GitSyncSettings` field name.

    A `None` clears that field's override (reverting to the env default)
    rather than being stored literally -- the settings fields are plain
    str/bool/int, so a literal `None` would corrupt `model_copy`. Secret
    fields are encrypted on the way to storage; see secret_store.py.
    """
    overrides = await _load_overrides(session)
    for key, value in updates.items():
        if value is None:
            overrides.pop(key, None)
        else:
            overrides[key] = value
    await _store_overrides(session, overrides)
