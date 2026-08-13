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
    # A cycle reports its outcome only once it finishes, so without these a
    # caller can't tell "running" from "nothing happened".
    sync_in_progress: bool = False
    sync_started_at: UtcDatetime | None = None


class GitSyncTriggerResponse(BaseModel):
    """`triggered` is False when a cycle was already running -- that cycle
    picks up everything this one would have, so the trigger is a no-op."""

    triggered: bool


class GitSyncCheckResponse(BaseModel):
    """Result of `POST /v1/git-sync/check`.

    `ok` means reachability, not correctness: git could list the remote's
    branches with these credentials. A token without write scope still passes,
    and the encryption key is never exercised.

    `branch_exists` False with `ok` True is normal for a never-synced repo --
    the first cycle creates the branch.
    """

    ok: bool
    branch_exists: bool = False
    # git's own failure output, with any credentials in the URL redacted.
    detail: str | None = None


class GitSyncConfigUpdateRequest(BaseModel):
    """Partial update for runtime git-sync config overrides.

    Omitted fields are unchanged; an explicit `null` clears that field's
    override, reverting to the env default (`interval_seconds` has no env var
    and defaults to 0). Can only reconfigure or pause an already-running sync,
    never enable one in a deployment that booted with it disabled.

    `interval_seconds` is how often to sync automatically; 0 is manual-only.

    String fields are stripped, and a blank is treated as `null`. The UI sends
    `""` for a cleared text field, and storing that would wedge sync: both
    `git checkout -B ""` and `git add -A -- ""` are fatal.
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
        # Raises for a traversing path, which FastAPI turns into a 422 rather
        # than letting it reach `sync_root` and the export's rmtree.
        return normalize_git_sync_path(value)
