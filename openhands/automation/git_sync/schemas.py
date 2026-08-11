"""Pydantic schemas for the git sync status API."""

from pydantic import BaseModel, Field, field_validator

from openhands.automation.config import normalize_git_sync_path
from openhands.automation.utils.time import UtcDatetime


class GitSyncStatusResponse(BaseModel):
    enabled: bool
    repo_url: str
    branch: str
    path: str
    encryption_enabled: bool
    interval_seconds: int
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
    clears its override and reverts it to the default (the env var for
    every field except `interval_seconds`, which has no env var and
    defaults to 0). Only reconfigures/pauses an already-running sync -- it
    can't newly enable sync in a deployment that booted with it disabled
    (that still needs AUTOMATION_GIT_SYNC_ENABLED + a restart).

    `interval_seconds` is how often to sync automatically; 0 means
    manual-only, i.e. sync just when POST /v1/git-sync/sync is called.

    Every string field is stripped, and a blank one is treated the same as
    `null` (clear the override). The UI sends `""` rather than `null` when a
    text field is cleared, and storing that verbatim would wedge sync: an
    empty branch makes `git checkout -B ""` fatal, and an empty path makes
    `git add -A -- ""` fatal.
    """

    enabled: bool | None = None
    interval_seconds: int | None = Field(default=None, ge=0)
    repo_url: str | None = None
    branch: str | None = None
    path: str | None = None
    token: str | None = None
    encryption_key: str | None = None
    author_name: str | None = None
    author_email: str | None = None

    @field_validator(
        "repo_url",
        "branch",
        "token",
        "encryption_key",
        "author_name",
        "author_email",
        mode="after",
    )
    @classmethod
    def _blank_clears_the_override(cls, value: str | None) -> str | None:
        return (value.strip() or None) if value is not None else None

    @field_validator("path", mode="after")
    @classmethod
    def _normalize_path(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        # Raises for a traversing path, which FastAPI reports as a 422 rather
        # than letting it reach `sync_root` and the export's rmtree.
        return normalize_git_sync_path(value)
