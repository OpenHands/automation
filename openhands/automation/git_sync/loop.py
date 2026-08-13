"""Bidirectional git sync for automations (local/self-hosted mode only).

Conflict policy: a `dirty` automation (created/updated/deleted via the API
since its last sync) wins over a conflicting git-side change in the same
cycle — the VM is the source of truth for anything not yet pushed.
"""

import asyncio
import logging
import shutil
import tarfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Final

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from openhands.automation.auth import _get_local_user
from openhands.automation.config import (
    GitSyncSettings,
    ServiceSettings,
    get_config,
    normalize_git_sync_path,
)
from openhands.automation.git_sync.client import (
    GitSyncError,
    commit_and_push,
    diff_names,
    ensure_repo,
    pull,
)
from openhands.automation.git_sync.config_override import (
    resolve_effective_git_sync_settings,
    resolve_effective_sync_interval_seconds,
)
from openhands.automation.git_sync.serializer import (
    METADATA_FILENAME,
    TARBALL_DIRNAME,
    DeserializedAutomation,
    canonical_tarball_bytes,
    compute_content_hash,
    compute_slug,
    decrypt_file_tree,
    deserialize_automation,
    encrypt_file_tree,
    is_generated_path,
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

# How often the loop re-reads config while sync is manual-only. This is the
# worst-case delay before an interval set from the UI takes effect, so it is
# kept short; the tick is a single indexed metadata read, not a sync.
_IDLE_POLL_SECONDS: Final[int] = 15

GIT_SYNC_LAST_COMMIT_KEY: Final[str] = "git_sync_last_commit"
GIT_SYNC_LAST_PATH_KEY: Final[str] = "git_sync_last_path"
GIT_SYNC_LAST_RUN_AT_KEY: Final[str] = "git_sync_last_run_at"
GIT_SYNC_LAST_ERROR_KEY: Final[str] = "git_sync_last_error"
GIT_SYNC_LAST_ERROR_AT_KEY: Final[str] = "git_sync_last_error_at"

_TRIGGER_ADAPTER: Final[TypeAdapter[Trigger]] = TypeAdapter(Trigger)


def is_git_sync_opted_in() -> bool:
    """Whether the deployment opted into git sync at boot: env flag + local mode.

    This is the gate for everything that can't be turned on at runtime: the
    background loop is only started when it holds, and `PUT /config` refuses
    to flip `enabled` on when it doesn't.

    Deliberately does NOT require a repo URL, unlike `is_git_sync_active`.
    The repo is routine, non-privileged configuration that the Git Sync page
    is meant to supply, so an operator can set `AUTOMATION_GIT_SYNC_ENABLED`
    and fill the rest in from the UI. Requiring it here would leave the loop
    unstarted and `mark_git_sync_dirty` a no-op for the whole process
    lifetime, so a repo configured from the UI would report a healthy sync
    while never exporting anything.

    Local mode is required because one repo maps to one agent server, which
    doesn't make sense for the multi-tenant SaaS.
    """
    config = get_config()
    return config.git_sync.git_sync_enabled and config.service.is_local_mode


def is_git_sync_active() -> bool:
    """Whether git sync is fully configured from env alone: opted in + a repo.

    Reads only the boot-time env config, so it says nothing about a repo
    configured at runtime. Callers that must honour the runtime overrides
    (PUT /v1/git-sync/config) have to resolve the effective settings
    themselves, as the router's `_is_effectively_enabled` does.
    """
    return is_git_sync_opted_in() and bool(get_config().git_sync.git_sync_repo_url)


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

    Gates on the boot-time opt-in, not on whether sync is fully configured
    and running. Requiring a repo URL here left dirty-marking permanently
    off when the repo was supplied from the Git Sync page instead of the
    environment, and gating on the effective `enabled` would drop every edit
    made while sync is paused -- both cases silently lose changes that a
    later cycle should have exported.
    """
    if not is_git_sync_opted_in():
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


def _prune_generated_files(directory: Path) -> None:
    """Delete the files `serialize_automation` owns, leaving the rest alone.

    Stale generated members (e.g. a tarball file dropped in a newer upload)
    must not linger across an export, but the slug directory lives in the
    user's repo: rmtree'ing all of it also deleted any README, .gitignore or
    notes they had committed next to the generated files -- and pushed that
    deletion back to them.

    The tarball directory is generated in its entirety, so it goes as a unit.
    """
    if not directory.is_dir():
        return

    metadata = directory / METADATA_FILENAME
    if metadata.is_symlink() or metadata.is_file():
        metadata.unlink()

    tarball_dir = directory / TARBALL_DIRNAME
    # is_symlink first: rmtree refuses to act on a symlink to a directory,
    # and unlink is what actually removes the link rather than its target.
    if tarball_dir.is_symlink():
        tarball_dir.unlink()
    elif tarball_dir.is_dir():
        shutil.rmtree(tarball_dir)


def _write_files(directory: Path, files: dict[str, bytes]) -> None:
    _prune_generated_files(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for rel_path, content in files.items():
        target = directory / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def _remove_exported_automation(directory: Path) -> None:
    """Remove an automation's exported files after it's deleted in the DB.

    Only the generated files go; the directory itself is removed just when
    that leaves it empty, so user files committed alongside aren't collateral
    damage of deleting the automation.
    """
    _prune_generated_files(directory)
    if directory.is_dir() and not any(directory.iterdir()):
        directory.rmdir()


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


async def _stored_tarball_matches(
    session: AsyncSession, automation: Automation, tarball_bytes: bytes
) -> bool:
    """Whether `automation`'s stored tarball already holds this content.

    Compares canonical rebuilds rather than raw bytes: the stored tarball
    came from a user upload with its own gzip framing, mtimes and member
    order, so byte equality would report "changed" for identical content.
    Both sides go through `rebuild_tarball`, which is deterministic.
    """
    try:
        current = await _read_tarball_bytes(automation, session)
    except TarballUnavailableError:
        return False
    if current is None:
        return False
    try:
        return canonical_tarball_bytes(current) == tarball_bytes
    except tarfile.TarError:
        return False


async def _delete_superseded_upload(session: AsyncSession, tarball_path: str) -> None:
    """Remove the upload an automation just stopped pointing at.

    Without this, every git-side edit that touches an automation with a
    tarball left its previous upload row and file live and referenced by
    nothing -- routine PR-based edits accumulated a full tarball copy per
    merge, with no reaper anywhere to collect them. Mirrors the cleanup
    `regenerate_preset_prompt_tarball` already does for prompt edits.
    """
    upload_id = parse_internal_upload_id(tarball_path)
    if upload_id is None:
        return
    result = await session.execute(
        select(TarballUpload).where(TarballUpload.id == upload_id)
    )
    upload = result.scalars().first()
    if upload is None or upload.deleted_at is not None:
        return

    file_removed = False
    try:
        await asyncio.to_thread(get_file_store().delete, upload.storage_path)
        file_removed = True
    except FileNotFoundError:
        file_removed = True
    except Exception:
        logger.exception(
            "Failed to delete superseded tarball at %s", upload.storage_path
        )
    # Only soft-delete once the file is confirmed gone: a failed delete
    # leaves the record live so the still-present file stays discoverable
    # for a later retry, instead of becoming a hidden orphan.
    if file_removed:
        upload.deleted_at = utcnow()


async def _resolve_tarball_path(
    session: AsyncSession,
    fields: dict,
    deserialized: DeserializedAutomation,
    slug: str,
    existing: Automation | None,
) -> str:
    if deserialized.tarball_bytes is not None:
        if existing is not None and await _stored_tarball_matches(
            session, existing, deserialized.tarball_bytes
        ):
            # Nothing changed under tarball/ -- this import is a YAML-only
            # edit (say, flipping `enabled`). Re-uploading here would write
            # a second full copy of identical content on every such edit.
            return existing.tarball_path

        local_user = _get_local_user()
        new_path = await _write_tarball_upload(
            session,
            local_user.user_id,
            local_user.org_id,
            deserialized.tarball_bytes,
            slug,
        )
        if existing is not None:
            await _delete_superseded_upload(session, existing.tarball_path)
        return new_path

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
    existing: Automation | None = None,
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
    tarball_path = await _resolve_tarball_path(
        session, fields, deserialized, slug, existing
    )

    return {
        "name": name,
        "model": fields.get("model"),
        "trigger": trigger.model_dump(),
        "entrypoint": entrypoint,
        "setup_script_path": setup_script_path,
        "timeout": timeout,
        "keep_alive": fields.get("keep_alive"),
        # `dict.get`'s default only applies when the key is absent. A hand
        # edit that leaves "enabled:" with nothing after it is valid YAML
        # that parses to None, and bool(None) would silently disable a live
        # automation on the next import.
        "enabled": True if fields.get("enabled") is None else bool(fields["enabled"]),
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
        session, deserialized.fields, deserialized, state.slug, existing=automation
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


def _list_slug_directories(sync_root: Path) -> dict[str, Path]:
    """Automation directories directly under `sync_root`, keyed by slug.

    Symlinks are excluded. `is_dir()` follows them, so a committed symlink
    (git stores those as mode 120000) pointing at a host directory would be
    treated as an automation directory -- and `_read_directory_files`' own
    symlink guard never fires for it, because every file rglob yields
    *through* the link reports `is_symlink() == False`. It would also wedge
    the export, where `shutil.rmtree` raises outright on a symlinked
    directory, failing every subsequent cycle.
    """
    if not sync_root.is_dir():
        return {}

    directories: dict[str, Path] = {}
    for path in sorted(sync_root.iterdir()):
        if path.is_symlink():
            logger.warning(
                "Skipping symlinked automation directory in git checkout: %s", path
            )
            continue
        if path.is_dir():
            directories[path.name] = path
    return directories


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
    all_dirs = _list_slug_directories(sync_root)

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
            # Each directory gets its own SAVEPOINT: a failure partway
            # through rolls back just its writes (a half-populated
            # Automation, an already-flushed TarballUpload) and leaves the
            # session usable for the remaining directories.
            async with session.begin_nested():
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
        except Exception:
            # Deliberately broad. Anything escaping this loop aborts the
            # whole cycle -- no export, no push -- and keeps doing so every
            # cycle until someone fixes that one directory by hand, so a
            # single unparseable automation must not take the other ones
            # down with it. Logged with a traceback, since reaching here
            # means an error shape we didn't anticipate.
            logger.exception(
                "Unexpected error importing automation directory %r from git; "
                "skipping it and continuing the cycle",
                slug,
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


async def _backfill_missing_states(session: AsyncSession) -> int:
    """Create state rows for live automations that have never had one.

    `mark_git_sync_dirty` only fires on an API create/update/delete, so every
    automation that already existed when git sync was switched on had no
    state row at all -- and the export only looks at dirty state rows. The
    cycle reported success, pushed nothing, and the UI showed a healthy sync
    against an empty repo; an automation appeared only once someone happened
    to edit it again. Anyone restoring from that repo silently lost the rest.

    New rows start dirty so this cycle's export writes them out.

    Runs every cycle, so the steady state (nothing missing) is one indexed
    NOT EXISTS query returning no rows -- the slug set is only materialized
    when there is actually something to backfill.
    """
    missing = (
        (
            await session.execute(
                select(Automation).where(
                    Automation.deleted_at.is_(None),
                    ~select(AutomationGitSyncState.automation_id)
                    .where(AutomationGitSyncState.automation_id == Automation.id)
                    .exists(),
                )
            )
        )
        .scalars()
        .all()
    )
    if not missing:
        return 0

    taken = set(
        (await session.execute(select(AutomationGitSyncState.slug))).scalars().all()
    )
    for automation in missing:
        slug = compute_slug(automation.name, automation.id, taken=taken)
        taken.add(slug)
        session.add(
            AutomationGitSyncState(automation_id=automation.id, slug=slug, dirty=True)
        )

    await session.flush()
    logger.info(
        "Backfilled %d automation(s) with no git-sync state; they will be "
        "exported in this cycle",
        len(missing),
    )
    return len(missing)


def _exported_content_is_current(
    directory: Path, files: dict[str, bytes], encryption_key: str
) -> bool:
    """Whether the generated files already on disk match `files`.

    Compares the decrypted plaintext, not the bytes on disk: Fernet uses a
    fresh IV per encryption, so re-encrypting identical content yields
    different ciphertext every time and every cycle would look changed.

    Only the serializer-owned paths are compared -- user files committed in
    the same directory are none of this comparison's business.
    """
    if not directory.is_dir():
        return False
    try:
        on_disk = {
            name: content
            for name, content in _read_directory_files(directory).items()
            if is_generated_path(name)
        }
        if encryption_key:
            on_disk = decrypt_file_tree(on_disk, encryption_key)
    except Exception:
        # Unreadable or undecryptable (a rotated key, a corrupted commit):
        # treat it as stale and rewrite rather than skipping the export.
        logger.warning(
            "Could not read the existing export at %s; rewriting it", directory
        )
        return False
    return compute_content_hash(on_disk) == compute_content_hash(files)


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
                await asyncio.to_thread(_remove_exported_automation, directory)
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

        # Compared against what's actually on disk, not against
        # `state.content_hash`. The import step skips a dirty slug precisely
        # so this export can overwrite git with the DB's version -- but a
        # git-side edit to that slug leaves the DB row (and so its hash)
        # untouched, so a hash comparison concluded there was nothing to
        # write. The edit was then neither imported nor overwritten, and
        # `last_commit` advanced past it: git kept the user's version, the DB
        # kept its own, and the two never reconciled.
        new_hash = compute_content_hash(files)
        if not await asyncio.to_thread(
            _exported_content_is_current, directory, files, encryption_key
        ):
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
_sync_cycle_lock: Final[asyncio.Lock] = asyncio.Lock()

# When the cycle currently holding that lock started, so `GET /status` can
# report a sync as running rather than leaving callers to infer it from
# `last_synced_at` moving. Deliberately in-process state, the same scope as
# the lock it shadows: a crash mid-cycle can't strand a "still running" flag
# the way a persisted one would.
_sync_started_at: datetime | None = None


def get_sync_started_at() -> datetime | None:
    """When the in-flight sync cycle started, or None if none is running."""
    return _sync_started_at


async def run_sync_cycle(
    session_factory: async_sessionmaker[AsyncSession],
    git_settings: GitSyncSettings,
    service_settings: ServiceSettings,
) -> SyncCycleResult:
    """Run one full pull -> import -> export -> push sync cycle."""
    global _sync_started_at
    async with _sync_cycle_lock:
        _sync_started_at = utcnow()
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
        finally:
            _sync_started_at = None


async def _run_sync_cycle_locked(
    session_factory: async_sessionmaker[AsyncSession],
    git_settings: GitSyncSettings,
    service_settings: ServiceSettings,
) -> SyncCycleResult:
    workdir = _resolve_workdir(git_settings, service_settings)
    timeout = git_settings.git_sync_git_timeout_seconds
    token = git_settings.git_sync_token
    branch = git_settings.git_sync_branch.strip()
    encryption_key = git_settings.git_sync_encryption_key

    # Re-validated here, not just at the API boundary: this also covers a
    # misconfigured AUTOMATION_GIT_SYNC_PATH, and `sync_root` is what the
    # export rmtree's per automation. Raising leaves the reason in
    # `git_sync_last_error` for the status endpoint instead of deleting host
    # directories that happen to share a name with an automation slug.
    sync_path = normalize_git_sync_path(git_settings.git_sync_path)
    sync_root = workdir / sync_path
    if not sync_root.resolve().is_relative_to(workdir.resolve()):
        raise GitSyncError(
            f"git sync path {git_settings.git_sync_path!r} resolves outside the "
            f"checkout at {workdir}"
        )
    if not branch:
        raise GitSyncError("git sync branch must not be empty")

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
        if last_path is not None and last_path != sync_path:
            await _handle_path_change(session, workdir, last_path, sync_path)
        await set_service_metadata(session, GIT_SYNC_LAST_PATH_KEY, sync_path)

        if head is not None and head != last_commit:
            await _import_from_git(
                session,
                workdir,
                sync_root,
                sync_path,
                last_commit,
                head,
                timeout,
                encryption_key,
                result,
            )

        # After the import, so automations that arrived from git in this same
        # cycle already have their state row and aren't re-created here.
        await _backfill_missing_states(session)

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
        sync_path,
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
        # Both halves of the error, or the status endpoint reports a
        # `last_error_at` timestamp with no `last_error` to go with it.
        await set_service_metadata(session, GIT_SYNC_LAST_ERROR_KEY, "")
        await set_service_metadata(session, GIT_SYNC_LAST_ERROR_AT_KEY, "")
        await session.commit()

    return result


async def git_sync_loop(
    session_factory: async_sessionmaker[AsyncSession],
    shutdown_event: asyncio.Event | None = None,
) -> None:
    """Background loop: periodically syncs automations with the git repo.

    Only meant to be started when `is_git_sync_opted_in()` is true — the
    caller (app.py) checks that before starting this task. Once running,
    each cycle re-resolves runtime config overrides (PUT /v1/git-sync/config)
    and no-ops if they've paused sync or no repo is configured yet, without
    killing this task.

    The sync interval is runtime-configurable, so this task runs even while
    sync is manual-only (interval 0): it idles, re-reading the interval every
    `_IDLE_POLL_SECONDS`, and starts syncing once a positive one is set --
    without a restart. It never syncs while the interval is 0.
    """
    config = get_config()
    git_settings = config.git_sync
    service_settings = config.service

    logger.info(
        "Git sync loop started (repo=%s branch=%s path=%s); interval is set "
        "from the UI, 0 means manual-only",
        git_settings.git_sync_repo_url,
        git_settings.git_sync_branch,
        git_settings.git_sync_path,
    )

    async def _next_interval() -> float:
        """How long to sleep before the next tick.

        While manual-only there is nothing to wait for, so fall back to a
        short idle poll -- that is what lets a newly-set interval take
        effect without a restart.
        """
        async with session_factory() as session:
            interval = await resolve_effective_sync_interval_seconds(session)
        return float(interval) if interval > 0 else float(_IDLE_POLL_SECONDS)

    async def _cycle() -> None:
        async with session_factory() as session:
            effective = await resolve_effective_git_sync_settings(session, git_settings)
            interval = await resolve_effective_sync_interval_seconds(session)
        if not (service_settings.is_local_mode and effective.enabled):
            logger.debug("Git sync paused via runtime config; skipping this cycle")
            return
        if interval <= 0:
            logger.debug("Git sync is manual-only; skipping this automatic cycle")
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
        interval_seconds=_next_interval,
        shutdown_event=shutdown_event,
        logger=logger,
        name="Git sync",
        on_error=_on_error,
    )
