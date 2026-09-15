"""Per-organization runtime config for git sync, on top of the env defaults.

Lets `PUT /v1/git-sync/config` configure, reconfigure or pause/resume an org's
sync without a restart. Each org's overrides are one JSON blob on its
`automation_git_sync_org_config` row; the router and the sync loop merge that
blob over `base_git_sync_settings()` to get the settings a cycle runs with.
"""

import hashlib
import json
import uuid
from typing import Any, Final

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.config import GitSyncSettings, get_config
from openhands.automation.db import using_sqlite
from openhands.automation.git_sync.secret_store import (
    decrypt_secret_fields,
    encrypt_secret_fields,
)
from openhands.automation.models import AutomationGitSyncOrgConfig


# Runtime-only config: no env var, set solely from the UI. It rides in the same
# blob but is not a GitSyncSettings field, so it is filtered out before merging.
SYNC_INTERVAL_OVERRIDE_KEY: Final[str] = "git_sync_interval_seconds"

# 0 means manual-only: nothing syncs until POST /v1/git-sync/sync is called.
DEFAULT_SYNC_INTERVAL_SECONDS: Final[int] = 0

# The fields that say *which* repo an org syncs to. Two orgs sharing all three
# would import each other's automations, so `PUT /config` refuses that.
_REPO_IDENTITY_FIELDS: Final[tuple[str, ...]] = (
    "git_sync_repo_url",
    "git_sync_branch",
    "git_sync_path",
)


def base_git_sync_settings() -> GitSyncSettings:
    """The env-level defaults an org's overrides are merged over.

    In local mode the env vars configure the one local org, as they always
    have. Elsewhere the deployment is shared by every org, so the fields that
    name a repo are blanked: an `AUTOMATION_GIT_SYNC_REPO_URL` set on a cloud
    deployment must not sync every org into one repo. The neutral defaults
    (branch, path, author, timeout) still apply to all of them.
    """
    config = get_config()
    if config.service.is_local_mode:
        return config.git_sync
    return config.git_sync.model_copy(
        update={
            "git_sync_repo_url": "",
            "git_sync_token": "",
            "git_sync_encryption_key": "",
        }
    )


async def get_org_config(
    session: AsyncSession, org_id: uuid.UUID
) -> AutomationGitSyncOrgConfig | None:
    return await session.get(AutomationGitSyncOrgConfig, org_id)


async def get_or_create_org_config(
    session: AsyncSession, org_id: uuid.UUID
) -> AutomationGitSyncOrgConfig:
    """The org's row, created empty on first use.

    Two workers can race to create it (a first `PUT /config` and the loop's
    tick, say). The INSERT runs in a SAVEPOINT so the loser's primary-key
    collision rolls back only that statement and it reads the winner's row.
    """
    row = await session.get(AutomationGitSyncOrgConfig, org_id)
    if row is not None:
        return row
    try:
        async with session.begin_nested():
            row = AutomationGitSyncOrgConfig(org_id=org_id, overrides="{}")
            session.add(row)
            await session.flush()
        return row
    except IntegrityError:
        row = await session.get(AutomationGitSyncOrgConfig, org_id)
        if row is None:  # pragma: no cover - the other writer rolled back
            raise
        return row


def _decode_overrides(row: AutomationGitSyncOrgConfig | None) -> dict[str, Any]:
    if row is None or not row.overrides:
        return {}
    return decrypt_secret_fields(json.loads(row.overrides))


def _merge(git_settings: GitSyncSettings, overrides: dict[str, Any]) -> GitSyncSettings:
    settings_overrides = {
        key: value
        for key, value in overrides.items()
        if key != SYNC_INTERVAL_OVERRIDE_KEY
    }
    if not settings_overrides:
        return git_settings
    return git_settings.model_copy(update=settings_overrides)


def _interval_from(overrides: dict[str, Any]) -> int:
    # A stored value that isn't a non-negative int is ignored rather than left
    # to crash or busy-spin the sync loop.
    value = overrides.get(SYNC_INTERVAL_OVERRIDE_KEY)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return DEFAULT_SYNC_INTERVAL_SECONDS
    return value


def effective_settings_for(row: AutomationGitSyncOrgConfig) -> GitSyncSettings:
    """The settings an already-loaded org row resolves to."""
    return _merge(base_git_sync_settings(), _decode_overrides(row))


def effective_interval_for(row: AutomationGitSyncOrgConfig) -> int:
    """Seconds between the org's automatic syncs; 0 means manual-only."""
    return _interval_from(_decode_overrides(row))


async def resolve_effective_git_sync_settings(
    session: AsyncSession, org_id: uuid.UUID
) -> GitSyncSettings:
    """Merge the org's persisted overrides over the env-var defaults."""
    return _merge(
        base_git_sync_settings(),
        _decode_overrides(await get_org_config(session, org_id)),
    )


async def resolve_candidate_git_sync_settings(
    session: AsyncSession, org_id: uuid.UUID, updates: dict[str, Any]
) -> GitSyncSettings:
    """The settings a `PUT /config` with `updates` would leave in place.

    The same merge as apply + resolve, but persisting nothing: `POST /check`
    tests a configuration before it is saved. Handles the None-clears-the-
    override case, where the effective value is the env default, not a blank.
    """
    overrides = _decode_overrides(await get_org_config(session, org_id))
    for key, value in updates.items():
        # Dropped here as it is on the way out of storage: not a settings
        # field, and `model_copy` would graft it onto the model unvalidated.
        if key == SYNC_INTERVAL_OVERRIDE_KEY:
            continue
        if value is None:
            overrides.pop(key, None)
        else:
            overrides[key] = value
    return _merge(base_git_sync_settings(), overrides)


async def resolve_effective_sync_interval_seconds(
    session: AsyncSession, org_id: uuid.UUID
) -> int:
    """Seconds between the org's automatic syncs; 0 means manual-only."""
    return _interval_from(_decode_overrides(await get_org_config(session, org_id)))


async def apply_git_sync_config_override(
    session: AsyncSession,
    org_id: uuid.UUID,
    updates: dict[str, Any],
    *,
    configured_by_user_id: uuid.UUID,
) -> None:
    """Persist partial overrides for the org, keyed by `GitSyncSettings` field.

    A `None` clears that field's override (reverting to the env default)
    rather than being stored literally -- the settings fields are plain
    str/bool/int, so a literal `None` would corrupt `model_copy`. Secret
    fields are encrypted on the way to storage; see secret_store.py.

    `configured_by_user_id` records who saved last; automations imported from
    git are created as that user (see loop.py).
    """
    row = await get_or_create_org_config(session, org_id)
    overrides = _decode_overrides(row)
    for key, value in updates.items():
        if value is None:
            overrides.pop(key, None)
        else:
            overrides[key] = value
    row.overrides = json.dumps(encrypt_secret_fields(overrides))
    row.configured_by_user_id = configured_by_user_id


def _normalize_repo_url(url: str) -> str:
    normalized = url.strip().lower().rstrip("/")
    return normalized.removesuffix(".git")


def _repo_identity(git_settings: GitSyncSettings) -> tuple[str, str, str]:
    return (
        _normalize_repo_url(git_settings.git_sync_repo_url),
        git_settings.git_sync_branch.strip(),
        git_settings.git_sync_path.strip().strip("/"),
    )


async def lock_repo_identity(session: AsyncSession, candidate: GitSyncSettings) -> None:
    """Serialise config writes that name the same repository, branch and path.

    `find_org_using_repo` followed by `apply_git_sync_config_override` is a
    check and then a write, so two orgs saving the same repo at once could
    both pass the check and both keep it. Row locks can't close that gap: the
    other org's row may not exist yet. A transaction-scoped advisory lock on
    the repo identity holds the second writer until the first commits, so its
    check sees the committed row. Released by the caller's commit. SQLite runs
    single-process and skips it (the same pattern as `conversations.py`).
    """
    if using_sqlite():
        return
    digest = hashlib.blake2b(
        "/".join(_repo_identity(candidate)).encode(), digest_size=8
    ).digest()
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)").bindparams(
            key=int.from_bytes(digest, "big", signed=True)
        )
    )


async def find_org_using_repo(
    session: AsyncSession,
    candidate: GitSyncSettings,
    *,
    exclude_org_id: uuid.UUID,
) -> uuid.UUID | None:
    """Another org already syncing `candidate`'s repo, branch and path, if any.

    Reads the plaintext identity fields straight from each blob, so no secret
    is unwrapped for a comparison. The table has one row per org that ever
    saved a config, so a scan is fine.
    """
    wanted = _repo_identity(candidate)
    base = base_git_sync_settings()
    rows = (
        (
            await session.execute(
                select(AutomationGitSyncOrgConfig).where(
                    AutomationGitSyncOrgConfig.org_id != exclude_org_id
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        raw = json.loads(row.overrides) if row.overrides else {}
        overrides = {
            key: raw[key]
            for key in _REPO_IDENTITY_FIELDS
            if isinstance(raw.get(key), str)
        }
        other = _merge(base, overrides)
        if other.git_sync_repo_url and _repo_identity(other) == wanted:
            return row.org_id
    return None
