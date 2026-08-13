"""Bidirectional git sync for automations (local/self-hosted mode only).

Submodules:
    client.py           Async wrapper around the `git` CLI
    serializer.py       Automation <-> git file-tree (de)serializer, encryption
    loop.py             Sync cycle, background loop, mark_git_sync_dirty hook
    config_override.py  Runtime config overrides (PUT /v1/git-sync/config)
    secret_store.py     At-rest encryption for the stored token and key
    router.py           FastAPI status/config/trigger API (/v1/git-sync/*)
    schemas.py          Request/response schemas for router.py
"""

from openhands.automation.git_sync.loop import (
    SyncCycleResult,
    git_sync_loop,
    is_git_sync_active,
    is_git_sync_supported,
    mark_git_sync_dirty,
    run_sync_cycle,
)

# Named `git_sync_router`: re-exporting it as `router` would shadow the
# `router` submodule as a package attribute.
from openhands.automation.git_sync.router import router as git_sync_router


__all__ = [
    "SyncCycleResult",
    "git_sync_loop",
    "git_sync_router",
    "is_git_sync_active",
    "is_git_sync_supported",
    "mark_git_sync_dirty",
    "run_sync_cycle",
]
