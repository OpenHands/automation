"""Pydantic schemas for the git sync status API."""

from pydantic import BaseModel

from openhands.automation.utils.time import UtcDatetime


class GitSyncStatusResponse(BaseModel):
    enabled: bool
    repo_url: str
    branch: str
    path: str
    encryption_enabled: bool
    last_synced_commit: str | None
    last_synced_at: UtcDatetime | None
    last_error: str | None
    last_error_at: UtcDatetime | None
    dirty_count: int


class GitSyncTriggerResponse(BaseModel):
    triggered: bool


class GitSyncConfigUpdateRequest(BaseModel):
    """Partial update for runtime git-sync config overrides.

    Omitted fields are left unchanged; a field explicitly set to `null`
    clears its override and reverts it to the env-var default. Only
    reconfigures/pauses an already-running sync -- it can't newly enable
    sync in a deployment that booted with it disabled (that still needs
    AUTOMATION_GIT_SYNC_ENABLED + a restart).
    """

    enabled: bool | None = None
    repo_url: str | None = None
    branch: str | None = None
    path: str | None = None
    token: str | None = None
    encryption_key: str | None = None
    author_name: str | None = None
    author_email: str | None = None
