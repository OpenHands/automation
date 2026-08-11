"""Bidirectional git sync for automations (local/self-hosted mode only).

Conflict policy: a `dirty` automation (created/updated/deleted via the API
since its last sync) wins over a conflicting git-side change in the same
cycle — the VM is the source of truth for anything not yet pushed.
"""

import asyncio
import logging
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from openhands.automation.auth import _get_local_user
from openhands.automation.config import GitSyncSettings, ServiceSettings, get_config
from openhands.automation.git_sync.client import (
    GitSyncError,
    commit_and_push,
    diff_names,
    ensure_repo,
    pull,
)
from openhands.automation.git_sync.config_override import (
    resolve_effective_git_sync_settings,
)
from openhands.automation.git_sync.serializer import (
    DeserializedAutomation,
    compute_content_hash,
    compute_slug,
    decrypt_file_tree,
    deserialize_automation,
    encrypt_file_tree,
    serialize_automation,
)
from openhands.automation.models import (
    Automation,
    AutomationGitSyncState,
    TarballUpload,
    UploadStatus,
)
from openhands.automation.schemas import Trigger, validate_command_string
from openhands.automation.storage import get_file_store
from openhands.automation.utils import utcnow
from openhands.automation.utils.periodic_loop import run_periodic_loop
from openhands.automation.utils.service_metadata import (
    get_service_metadata,
    set_service_metadata,
)
from openhands.automation.utils.tarball_validation import (
    build_internal_url,
    build_upload_storage_path,
    is_valid_external_url,
    parse_internal_upload_id,
)
from openhands.automation.utils.timeout import validate_automation_timeout


logger = logging.getLogger("automation.git_sync")

GIT_SYNC_LAST_COMMIT_KEY = "git_sync_last_commit"
GIT_SYNC_LAST_PATH_KEY = "git_sync_last_path"
GIT_SYNC_LAST_RUN_AT_KEY = "git_sync_last_run_at"
GIT_SYNC_LAST_ERROR_KEY = "git_sync_last_error"
GIT_SYNC_LAST_ERROR_AT_KEY = "git_sync_last_error_at"

_TRIGGER_ADAPTER: TypeAdapter = TypeAdapter(Trigger)


def is_git_sync_active() -> bool:
    """Whether git sync should be active: enabled, with a repo, and local mode.

    Local mode is required because one repo maps to one agent server, which
    doesn't make sense for the multi-tenant SaaS.
    """
    config = get_config()
    return config.git_sync.enabled and config.service.is_local_mode


@dataclass
class SyncCycleResult:
    head: str | None
    imported: int = field(default=0)
    deleted_in_db: int = field(default=0)
    exported: int = field(default=0)
    deleted_in_git: int = field(default=0)
    pushed_commit: str | None = field(default=None)


# --- CRUD hook -------------------------------------------------------------


async def mark_git_sync_dirty(session: AsyncSession, automation: Automation) -> None:
    """Flag an automation as needing (re)export on the next sync cycle.

    Best-effort: no-ops if git sync isn't active, and swallows unexpected
    errors (inside a SAVEPOINT, so the caller's own pending changes
    survive) since some callers treat any exception here as a full
    automation-creation failure.
    """
    if not is_git_sync_active():
        return

    try:
        async with session.begin_nested():
            await _mark_git_sync_dirty_inner(session, automation)
    except Exception:
        logger.exception(
            "Failed to mark automation %s dirty for git sync; it will not "
            "be synced until its next update",
            automation.id,
        )


async def _mark_git_sync_dirty_inner(
    session: AsyncSession, automation: Automation
) -> None:
    result = await session.execute(
        select(AutomationGitSyncState).where(
            AutomationGitSyncState.automation_id == automation.id
        )
    )
    state = result.scalars().first()
    if state is not None:
        state.dirty = True
        return

    # A UNIQUE-constraint collision (e.g. two automations with the same
    # name) rolls back just this inner savepoint; retry once with an
    # id-suffixed slug, which is guaranteed free.
    base_slug = compute_slug(automation.name, automation.id, taken=set())
    fallback_slug = compute_slug(automation.name, automation.id, taken={base_slug})
    for slug in (base_slug, fallback_slug):
        try:
            async with session.begin_nested():
                session.add(
                    AutomationGitSyncState(
                        automation_id=automation.id, slug=slug, dirty=True
                    )
                )
                await session.flush()
            return
        except IntegrityError:
            continue

    logger.error(
        "Could not allocate a unique git-sync slug for automation %s "
        "(base slug %r collided twice); leaving it unsynced for now",
        automation.id,
        base_slug,
    )


# --- Filesystem helpers ------------------------------------------------------


def _resolve_workdir(
    git_settings: GitSyncSettings, service_settings: ServiceSettings
) -> Path:
    if git_settings.git_sync_local_workdir:
        return Path(git_settings.git_sync_local_workdir)
    return Path(service_settings.workspace_base) / "git-sync"


def _read_directory_files(directory: Path) -> dict[str, bytes]:
    """Read every regular file under `directory` into a `{rel_path: bytes}` map.

    Skips symlinks -- `is_file()`/`read_bytes()` follow them by default, and
    a git commit can contain one pointing outside the checkout, letting a
    malicious push exfiltrate host files via an imported automation.
    """
    files: dict[str, bytes] = {}
    for path in directory.rglob("*"):
        if path.is_symlink():
            logger.warning("Skipping symlink in git checkout: %s", path)
            continue
        if path.is_file():
            files[path.relative_to(directory).as_posix()] = path.read_bytes()
    return files


def _write_files(directory: Path, files: dict[str, bytes]) -> None:
    # Remove first so stale members (e.g. a tarball file dropped in a newer
    # upload) don't linger from a previous export.
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for rel_path, content in files.items():
        target = directory / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


# --- Tarball storage helpers --------------------------------------------------


async def _bytes_to_async_iter(data: bytes):
    yield data


class TarballUnavailableError(Exception):
    """Raised when an internal automation's tarball can't be read from storage."""


async def _read_tarball_bytes(
    automation: Automation, session: AsyncSession
) -> bytes | None:
    """Fetch tarball bytes for an internal-upload automation, else None.

    `None` means the automation genuinely has no tarball (external URL).
    Raises `TarballUnavailableError` when an internal automation's tarball
    can't be read -- callers must not treat that the same as "no tarball",
    which would overwrite already-synced content in git.
    """
    upload_id = parse_internal_upload_id(automation.tarball_path)
    if upload_id is None:
        return None
    result = await session.execute(
        select(TarballUpload).where(TarballUpload.id == upload_id)
    )
    upload = result.scalars().first()
    if upload is None:
        raise TarballUnavailableError(
            f"automation {automation.id} references missing upload {upload_id}"
        )
    try:
        return await asyncio.to_thread(get_file_store().read, upload.storage_path)
    except FileNotFoundError as e:
        raise TarballUnavailableError(
            f"tarball for automation {automation.id} not found in storage "
            f"at {upload.storage_path!r}"
        ) from e


async def _write_tarball_upload(
    session: AsyncSession,
    user_id: uuid.UUID,
    org_id: uuid.UUID,
    tarball_bytes: bytes,
    name: str,
) -> str:
    """Upload tarball bytes as a new internal upload; return its oh-internal:// URL."""
    upload_id = uuid.uuid4()
    storage_path = build_upload_storage_path(org_id, user_id, upload_id)
    upload = TarballUpload(
        id=upload_id,
        user_id=user_id,
        org_id=org_id,
        name=f"git-sync-{name}"[:255],
        description="Imported from git sync",
        status=UploadStatus.UPLOADING,
        storage_path=storage_path,
    )
    session.add(upload)
    await session.flush()

    file_store = get_file_store()
    size_bytes = await file_store.write_stream(
        path=storage_path,
        stream=_bytes_to_async_iter(tarball_bytes),
        content_type="application/x-tar",
    )
    upload.status = UploadStatus.COMPLETED
    upload.size_bytes = size_bytes
    return build_internal_url(upload_id)


async def _resolve_tarball_path(
    session: AsyncSession,
    fields: dict,
    deserialized: DeserializedAutomation,
    slug: str,
) -> str:
    if deserialized.tarball_bytes is not None:
        local_user = _get_local_user()
        return await _write_tarball_upload(
            session,
            local_user.user_id,
            local_user.org_id,
            deserialized.tarball_bytes,
            slug,
        )

    tarball_source = fields.get("tarball_source") or {}
    url = tarball_source.get("url")
    if tarball_source.get("type") == "external" and url and is_valid_external_url(url):
        return url

    raise ValueError(
        f"automation directory {slug!r} has no tarball/ contents and no valid "
        "external tarball_source.url"
    )


# --- Import (git -> DB) -------------------------------------------------------


async def _validate_and_resolve_fields(
    session: AsyncSession,
    fields: dict,
    deserialized: DeserializedAutomation,
    slug: str,
) -> dict:
    """Validate automation.yaml fields and resolve a tarball_path, mirroring
    the API request schemas' validation (schemas.py).

    Returns a plain dict rather than mutating an Automation as fields are
    parsed, so a failure partway through can't leave one field applied and
    another not.
    """
    name = fields.get("name")
    raw_entrypoint = fields.get("entrypoint")
    if not name or not raw_entrypoint:
        raise ValueError("automation.yaml missing required 'name' or 'entrypoint'")

    trigger = _TRIGGER_ADAPTER.validate_python(fields.get("trigger") or {})
    entrypoint = validate_command_string(raw_entrypoint, "entrypoint", allow_none=False)
    setup_script_path = validate_command_string(
        fields.get("setup_script_path"), "setup_script_path"
    )
    timeout = validate_automation_timeout(fields.get("timeout"))
    tarball_path = await _resolve_tarball_path(session, fields, deserialized, slug)

    return {
        "name": name,
        "model": fields.get("model"),
        "trigger": trigger.model_dump(),
        "entrypoint": entrypoint,
        "setup_script_path": setup_script_path,
        "timeout": timeout,
        "keep_alive": fields.get("keep_alive"),
        "enabled": bool(fields.get("enabled", True)),
        "prompt": fields.get("prompt"),
        "preset_metadata": fields.get("preset_metadata"),
        "tarball_path": tarball_path,
    }


async def _create_automation_from_git(
    session: AsyncSession,
    slug: str,
    deserialized: DeserializedAutomation,
    dir_files: dict[str, bytes],
    head: str,
) -> None:
    values = await _validate_and_resolve_fields(
        session, deserialized.fields, deserialized, slug
    )
    local_user = _get_local_user()
    automation = Automation(
        user_id=local_user.user_id, org_id=local_user.org_id, **values
    )
    session.add(automation)
    await session.flush()

    session.add(
        AutomationGitSyncState(
            automation_id=automation.id,
            slug=slug,
            content_hash=compute_content_hash(dir_files),
            last_synced_commit=head,
            last_synced_at=utcnow(),
            dirty=False,
        )
    )
    logger.info("Imported new automation %s from git (slug=%r)", automation.id, slug)


async def _update_automation_from_git(
    session: AsyncSession,
    state: AutomationGitSyncState,
    deserialized: DeserializedAutomation,
    dir_files: dict[str, bytes],
    head: str,
) -> None:
    new_hash = compute_content_hash(dir_files)
    if new_hash == state.content_hash:
        # No real content change -- most commonly, this is the commit we
        # just pushed ourselves in a prior cycle's export step.
        state.last_synced_commit = head
        return

    automation = await session.get(Automation, state.automation_id)
    if automation is None:
        raise ValueError(
            f"automation {state.automation_id} not found for slug {state.slug!r}"
        )

    values = await _validate_and_resolve_fields(
        session, deserialized.fields, deserialized, state.slug
    )
    for column, value in values.items():
        setattr(automation, column, value)

    state.content_hash = new_hash
    state.last_synced_commit = head
    state.last_synced_at = utcnow()
    logger.info("Updated automation %s from git (slug=%r)", automation.id, state.slug)


async def _changed_slugs_since(
    workdir: Path,
    sync_path: str,
    last_commit: str | None,
    head: str,
    timeout: float,
) -> set[str] | None:
    """Slugs whose directory changed between `last_commit` and `head`.

    Returns `None` ("consider every directory") when there's no usable base
    to diff from -- the first sync cycle, or a shallow clone/history
    rewrite -- so a degraded checkout still imports correctly.
    """
    if last_commit is None:
        return None
    try:
        changed_paths = await diff_names(workdir, sync_path, last_commit, head, timeout)
    except GitSyncError:
        logger.warning(
            "Could not diff %s..%s under %r; falling back to a full import scan",
            last_commit,
            head,
            sync_path,
        )
        return None

    prefix = f"{sync_path}/"
    slugs: set[str] = set()
    for changed_path in changed_paths:
        if not changed_path.startswith(prefix):
            continue
        slug = changed_path[len(prefix) :].split("/", 1)[0]
        if slug:
            slugs.add(slug)
    return slugs


async def _import_from_git(
    session: AsyncSession,
    workdir: Path,
    sync_root: Path,
    sync_path: str,
    last_commit: str | None,
    head: str,
    timeout: float,
    encryption_key: str,
    result: SyncCycleResult,
) -> None:
    all_dirs = (
        {p.name: p for p in sorted(sync_root.iterdir()) if p.is_dir()}
        if sync_root.is_dir()
        else {}
    )

    # Only re-read/hash slugs that actually changed in this commit range.
    changed_slugs = await _changed_slugs_since(
        workdir, sync_path, last_commit, head, timeout
    )
    dirs_to_process = (
        all_dirs
        if changed_slugs is None
        else {slug: d for slug, d in all_dirs.items() if slug in changed_slugs}
    )

    states = (await session.execute(select(AutomationGitSyncState))).scalars().all()
    states_by_slug = {s.slug: s for s in states}

    for slug, directory in dirs_to_process.items():
        state = states_by_slug.get(slug)
        if state is not None and state.dirty:
            # VM wins this cycle; the export step will overwrite git with
            # the DB's current content for this automation.
            continue

        dir_files = await asyncio.to_thread(_read_directory_files, directory)
        try:
            if encryption_key:
                dir_files = decrypt_file_tree(dir_files, encryption_key)
            deserialized = deserialize_automation(dir_files)
            if deserialized is None:
                continue

            if state is None:
                await _create_automation_from_git(
                    session, slug, deserialized, dir_files, head
                )
                result.imported += 1
            else:
                await _update_automation_from_git(
                    session, state, deserialized, dir_files, head
                )
        except (ValidationError, ValueError) as e:
            logger.warning(
                "Skipping invalid automation directory %r from git: %s", slug, e
            )

    # Known automations whose directory disappeared. Checked against
    # `all_dirs`, not the diff-scoped `dirs_to_process` -- an unchanged
    # directory must not be mistaken for "deleted".
    for slug, state in states_by_slug.items():
        if slug in all_dirs or state.dirty:
            continue
        if changed_slugs is not None and slug not in changed_slugs:
            # Missing but untouched by this range -- a pre-existing
            # inconsistency, not a fresh deletion. A full scan will catch it.
            continue
        automation = await session.get(Automation, state.automation_id)
        if automation is not None and automation.deleted_at is None:
            automation.enabled = False
            automation.deleted_at = utcnow()
            result.deleted_in_db += 1
            logger.info("Soft-deleted automation %s (removed from git)", automation.id)
        await session.delete(state)


async def _handle_path_change(
    session: AsyncSession, workdir: Path, old_path: str, new_path: str
) -> None:
    """Re-export every automation after `git_sync_path` changes.

    The automations themselves didn't change, so none of them are dirty and
    the export step would write nothing under the new location -- the new
    path would stay empty while the old one silently kept serving stale
    copies. Marking them dirty makes the export rewrite all of them there
    (`_export_dirty_automations` already writes whenever the target
    directory is missing, regardless of content hash).

    Doubles as protection for the import step, which runs first: a
    non-dirty state whose directory is absent from the still-empty new path
    would otherwise be read as an automation deleted in git and soft-deleted.

    The old directory is deliberately left in place rather than deleted --
    removing files from the user's repo on a config change is not this
    loop's call to make -- so it is reported loudly instead.
    """
    await session.execute(update(AutomationGitSyncState).values(dirty=True))

    old_root = workdir / old_path
    leftover = (
        sum(1 for entry in old_root.iterdir() if entry.is_dir())
        if old_root.is_dir()
        else 0
    )
    logger.warning(
        "git-sync path changed (%r -> %r); re-exporting all automations "
        "under the new path. %d automation director(y/ies) remain at %r, "
        "are no longer synced, and must be deleted manually if unwanted.",
        old_path,
        new_path,
        leftover,
        old_path,
    )


# --- Export (DB -> git) -------------------------------------------------------


async def _export_dirty_automations(
    session: AsyncSession,
    sync_root: Path,
    encryption_key: str,
    result: SyncCycleResult,
) -> bool:
    states = (
        (
            await session.execute(
                select(AutomationGitSyncState).where(
                    AutomationGitSyncState.dirty.is_(True)
                )
            )
        )
        .scalars()
        .all()
    )
    if not states:
        return False

    changed = False
    for state in states:
        automation = await session.get(Automation, state.automation_id)
        directory = sync_root / state.slug

        if automation is None or automation.deleted_at is not None:
            if directory.exists():
                await asyncio.to_thread(shutil.rmtree, directory)
                changed = True
            await session.delete(state)
            result.deleted_in_git += 1
            continue

        try:
            tarball_bytes = await _read_tarball_bytes(automation, session)
            files = serialize_automation(automation, tarball_bytes)
        except Exception:
            logger.exception(
                "Failed to serialize automation %s for git sync", automation.id
            )
            continue

        # Hash the plaintext, not what's written to disk -- Fernet's random
        # per-encryption IV would otherwise make every cycle look "changed"
        # even with no real content change, and trigger a needless commit.
        new_hash = compute_content_hash(files)
        if new_hash != state.content_hash or not directory.is_dir():
            on_disk_files = (
                encrypt_file_tree(files, encryption_key) if encryption_key else files
            )
            await asyncio.to_thread(_write_files, directory, on_disk_files)
            changed = True

        state.content_hash = new_hash
        state.dirty = False
        result.exported += 1

    return changed


# --- Cycle + loop --------------------------------------------------------------

# Serializes cycles so the periodic loop and a manual trigger
# (POST /v1/git-sync/sync) never race on the same git workdir.
_sync_cycle_lock = asyncio.Lock()


async def run_sync_cycle(
    session_factory: async_sessionmaker[AsyncSession],
    git_settings: GitSyncSettings,
    service_settings: ServiceSettings,
) -> SyncCycleResult:
    """Run one full pull -> import -> export -> push sync cycle."""
    async with _sync_cycle_lock:
        try:
            return await _run_sync_cycle_locked(
                session_factory, git_settings, service_settings
            )
        except Exception as e:
            async with session_factory() as session:
                await set_service_metadata(
                    session, GIT_SYNC_LAST_ERROR_KEY, str(e)[:2000]
                )
                await set_service_metadata(
                    session, GIT_SYNC_LAST_ERROR_AT_KEY, utcnow().isoformat()
                )
                await session.commit()
            raise


async def _run_sync_cycle_locked(
    session_factory: async_sessionmaker[AsyncSession],
    git_settings: GitSyncSettings,
    service_settings: ServiceSettings,
) -> SyncCycleResult:
    workdir = _resolve_workdir(git_settings, service_settings)
    timeout = git_settings.git_sync_git_timeout_seconds
    token = git_settings.git_sync_token
    branch = git_settings.git_sync_branch
    encryption_key = git_settings.git_sync_encryption_key
    sync_root = workdir / git_settings.git_sync_path

    await ensure_repo(workdir, git_settings.git_sync_repo_url, branch, token, timeout)
    head = await pull(workdir, branch, token, timeout)

    result = SyncCycleResult(head=head)

    # Committed *before* the push below, not held open across it -- SQLite
    # holds its single-writer lock for the whole transaction, and the push
    # can take up to git_sync_git_timeout_seconds.
    async with session_factory() as session:
        last_commit = await get_service_metadata(session, GIT_SYNC_LAST_COMMIT_KEY)

        # Before the import: a path change leaves the new sync_root empty,
        # and the import's "directory disappeared" branch would read that as
        # every automation having been deleted in git.
        last_path = await get_service_metadata(session, GIT_SYNC_LAST_PATH_KEY)
        if last_path is not None and last_path != git_settings.git_sync_path:
            await _handle_path_change(
                session, workdir, last_path, git_settings.git_sync_path
            )
        await set_service_metadata(
            session, GIT_SYNC_LAST_PATH_KEY, git_settings.git_sync_path
        )

        if head is not None and head != last_commit:
            await _import_from_git(
                session,
                workdir,
                sync_root,
                git_settings.git_sync_path,
                last_commit,
                head,
                timeout,
                encryption_key,
                result,
            )

        # Return value intentionally unused: the export counters land on
        # `result`, and the push below must run regardless of whether
        # anything was exported this cycle (see comment there).
        await _export_dirty_automations(session, sync_root, encryption_key, result)
        await session.commit()

    # Called unconditionally, not gated on `exported`: commit_and_push is
    # also what retries a commit that a previous cycle created but failed to
    # push, and what pushes an existing local commit to a newly-repointed
    # remote (git_sync_repo_url changed at runtime). Gating on `exported`
    # made that recovery path unreachable whenever there was nothing new to
    # export -- the cycle would report success while silently never pushing.
    # It self-limits: with nothing staged and nothing pending it returns None
    # without touching the network.
    pushed = await commit_and_push(
        workdir,
        git_settings.git_sync_path,
        "Sync automations from agent server",
        git_settings.git_sync_author_name,
        git_settings.git_sync_author_email,
        branch,
        token,
        timeout,
    )
    result.pushed_commit = pushed

    new_head = pushed or head
    async with session_factory() as session:
        if new_head:
            await set_service_metadata(session, GIT_SYNC_LAST_COMMIT_KEY, new_head)
        await set_service_metadata(
            session, GIT_SYNC_LAST_RUN_AT_KEY, utcnow().isoformat()
        )
        await set_service_metadata(session, GIT_SYNC_LAST_ERROR_KEY, "")
        await session.commit()

    return result


async def git_sync_loop(
    session_factory: async_sessionmaker[AsyncSession],
    shutdown_event: asyncio.Event | None = None,
) -> None:
    """Background loop: periodically syncs automations with the git repo.

    Only meant to be started when `is_git_sync_active()` is true — the
    caller (app.py) checks that before starting this task. Once running,
    each cycle re-resolves runtime config overrides (PUT /v1/git-sync/config)
    and no-ops if they've paused sync, without killing this task.

    Returns immediately when the poll interval is 0 (the default), leaving
    sync to run only when triggered via POST /v1/git-sync/sync.
    """
    config = get_config()
    git_settings = config.git_sync
    service_settings = config.service
    interval = git_settings.git_sync_interval_seconds

    if interval <= 0:
        logger.info(
            "Git sync is manual-only (interval=%s); syncing only when "
            "POST /v1/git-sync/sync is called",
            interval,
        )
        return

    logger.info(
        "Git sync started, polling every %ds (repo=%s branch=%s path=%s)",
        interval,
        git_settings.git_sync_repo_url,
        git_settings.git_sync_branch,
        git_settings.git_sync_path,
    )

    async def _cycle() -> None:
        async with session_factory() as session:
            effective = await resolve_effective_git_sync_settings(session, git_settings)
        if not (service_settings.is_local_mode and effective.enabled):
            logger.debug("Git sync paused via runtime config; skipping this cycle")
            return

        result = await run_sync_cycle(session_factory, effective, service_settings)
        if any(
            (
                result.imported,
                result.exported,
                result.deleted_in_db,
                result.deleted_in_git,
            )
        ):
            logger.info(
                "Git sync cycle complete: imported=%d exported=%d "
                "deleted_in_db=%d deleted_in_git=%d pushed=%s",
                result.imported,
                result.exported,
                result.deleted_in_db,
                result.deleted_in_git,
                result.pushed_commit,
            )

    def _on_error(e: Exception) -> None:
        if isinstance(e, GitSyncError):
            logger.exception("Git sync cycle failed")
        else:
            logger.exception("Unexpected error in git sync cycle")

    await run_periodic_loop(
        _cycle,
        interval_seconds=interval,
        shutdown_event=shutdown_event,
        logger=logger,
        name="Git sync",
        on_error=_on_error,
    )
