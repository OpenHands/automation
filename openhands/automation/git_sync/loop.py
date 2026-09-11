"""Bidirectional git sync for automations, one repo per organization.

Conflict policy: a `dirty` automation -- one changed via the API since its last
sync -- wins over a conflicting git-side change in the same cycle. The VM is
the source of truth for anything not yet pushed.
"""

import asyncio
import logging
import shutil
import tarfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, NamedTuple

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import or_, select, update
from sqlalchemy.engine import CursorResult
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
    base_git_sync_settings,
    effective_interval_for,
    effective_settings_for,
    get_or_create_org_config,
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
    AutomationGitSyncOrgConfig,
    AutomationGitSyncState,
    TarballUpload,
    UploadStatus,
)
from openhands.automation.schemas import Trigger, validate_command_string
from openhands.automation.storage import ObjectNotFoundError, get_file_store
from openhands.automation.utils import utcnow
from openhands.automation.utils.periodic_loop import run_periodic_loop
from openhands.automation.utils.tarball_validation import (
    build_internal_url,
    build_upload_storage_path,
    is_valid_external_url,
    parse_internal_upload_id,
)
from openhands.automation.utils.time import ensure_utc
from openhands.automation.utils.timeout import validate_automation_timeout


logger = logging.getLogger("automation.git_sync")

# How often the loop re-reads config while manual-only -- the worst-case delay
# before a UI-set interval takes effect. Each tick is one indexed metadata read.
_IDLE_POLL_SECONDS: Final[int] = 15

# A crashed cycle leaves its org's lease set. Past this age it reads as expired
# so the org isn't blocked forever. Generous relative to a cycle, which is a
# handful of git commands each bounded by git_sync_git_timeout_seconds.
_SYNC_LEASE_MIN_SECONDS: Final[int] = 300
_SYNC_LEASE_TIMEOUT_MULTIPLIER: Final[int] = 5

_TRIGGER_ADAPTER: Final[TypeAdapter[Trigger]] = TypeAdapter(Trigger)


def is_git_sync_active() -> bool:
    """Whether git sync is on from env alone: a repo is set and not paused.

    Reads only boot-time env config, so it says nothing about an org's
    runtime-configured repo; and outside local mode the env-level repo is
    ignored (see `base_git_sync_settings`), so this is False there. Callers
    needing an org's overrides must resolve the effective settings themselves,
    as the router does.
    """
    return base_git_sync_settings().enabled


@dataclass
class SyncCycleResult:
    head: str | None
    imported: int = field(default=0)
    deleted_in_db: int = field(default=0)
    exported: int = field(default=0)
    deleted_in_git: int = field(default=0)
    pushed_commit: str | None = field(default=None)
    # True when another cycle held the org's lease, so nothing ran.
    skipped: bool = field(default=False)


# --- CRUD hook -------------------------------------------------------------


async def mark_git_sync_dirty(session: AsyncSession, automation: Automation) -> None:
    """Flag an automation as needing (re)export on the next sync cycle.

    Best-effort: swallows errors inside a SAVEPOINT so the caller's pending
    changes survive -- some callers treat any exception here as a failed
    automation creation.

    Not gated on the org having sync configured. Requiring a repo URL disabled
    dirty-marking whenever the repo came from the UI, and gating on the
    effective `enabled` would drop every edit made while sync is paused; both
    silently lose changes a later cycle should export. An org that never
    configures sync just carries one small state row per automation.
    """
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

    # A UNIQUE collision (two automations with the same name) rolls back just
    # this savepoint; retry once with an id-suffixed slug, guaranteed free.
    base_slug = compute_slug(automation.name, automation.id, taken=set())
    fallback_slug = compute_slug(automation.name, automation.id, taken={base_slug})
    for slug in (base_slug, fallback_slug):
        try:
            async with session.begin_nested():
                session.add(
                    AutomationGitSyncState(
                        automation_id=automation.id,
                        org_id=automation.org_id,
                        slug=slug,
                        dirty=True,
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
    git_settings: GitSyncSettings, service_settings: ServiceSettings, org_id: uuid.UUID
) -> Path:
    """Where this org's checkout lives.

    Local mode has one org and keeps the historical layout, so an existing
    checkout is reused. Elsewhere each org gets its own directory: the pod's
    disk is shared by every org it syncs.
    """
    if git_settings.git_sync_local_workdir:
        root = Path(git_settings.git_sync_local_workdir)
    else:
        root = Path(service_settings.workspace_base) / "git-sync"
    if service_settings.is_local_mode:
        return root
    return root / str(org_id)


def _read_directory_files(directory: Path) -> dict[str, bytes]:
    """Read every regular file under `directory` into `{rel_path: bytes}`.

    Skips symlinks: `is_file()`/`read_bytes()` follow them, and a commit can
    contain one pointing outside the checkout, letting a malicious push
    exfiltrate host files through an imported automation.
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

    Stale generated files (e.g. one dropped by a newer upload) must not linger
    across an export, but the slug directory is the user's: rmtree'ing it also
    deleted any README or notes they had committed there, and pushed that
    deletion back. The tarball directory is fully generated, so it goes as one.
    """
    if not directory.is_dir():
        return

    metadata = directory / METADATA_FILENAME
    if metadata.is_symlink() or metadata.is_file():
        metadata.unlink()

    tarball_dir = directory / TARBALL_DIRNAME
    # is_symlink first: rmtree refuses to act on a symlinked directory, and
    # unlink removes the link rather than its target.
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

    Only generated files go, and the directory only when that leaves it empty,
    so user files committed alongside aren't collateral damage.
    """
    _prune_generated_files(directory)
    if directory.is_dir() and not any(directory.iterdir()):
        directory.rmdir()


# --- Import owner -------------------------------------------------------------


class _Owner(NamedTuple):
    """Identity an automation created from git is stamped with."""

    user_id: uuid.UUID
    org_id: uuid.UUID


def _import_owner(org_config: AutomationGitSyncOrgConfig) -> _Owner | None:
    """Who owns automations created from git for this org, if anyone can.

    The admin who last saved the org's Git Sync config: an automation runs as
    its owner, and in cloud mode that means minting the owner's API key, so it
    has to be a real member of the org. Local mode has no such user and keeps
    its deterministic local identity. `None` -- an org row never saved from
    the UI, outside local mode -- makes the import skip new directories (see
    `_create_automation_from_git`); already-synced automations keep their
    owner and update as usual.
    """
    if org_config.configured_by_user_id is not None:
        return _Owner(org_config.configured_by_user_id, org_config.org_id)
    if get_config().service.is_local_mode:
        local = _get_local_user()
        return _Owner(local.user_id, local.org_id)
    return None


# --- Tarball storage helpers --------------------------------------------------


async def _bytes_to_async_iter(data: bytes):
    yield data


class TarballUnavailableError(Exception):
    """Raised when an internal automation's tarball can't be read from storage."""


async def _read_tarball_bytes(
    automation: Automation, session: AsyncSession
) -> bytes | None:
    """Fetch tarball bytes for an internal-upload automation, else None.

    `None` means there genuinely is no tarball (external URL). An unreadable
    internal tarball raises `TarballUnavailableError` instead -- treating that
    as "no tarball" would overwrite already-synced content in git.
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
    """Store tarball bytes as a new upload; return its oh-internal:// URL."""
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

    Compares canonical rebuilds, not raw bytes: the stored upload has its own
    gzip framing, mtimes and member order, so byte equality would report
    "changed" for identical content.
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


async def _delete_superseded_upload(
    session: AsyncSession, tarball_path: str, pending_storage_deletes: list[str]
) -> None:
    """Mark the upload an automation just stopped pointing at for removal.

    Without this, every git-side edit left the previous upload row and file
    referenced by nothing, so routine PR edits accumulated a tarball copy per
    merge with no reaper to collect them. Mirrors the cleanup
    `regenerate_preset_prompt_tarball` does for prompt edits.

    Soft-deletes the record in the current transaction; the storage object is
    only queued on `pending_storage_deletes` and removed after the cycle's
    commit. Deleting before the commit destroyed the object irreversibly while
    a rollback revived the record, stranding it pointing at a missing object.
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

    upload.deleted_at = utcnow()
    pending_storage_deletes.append(upload.storage_path)


async def _delete_pending_storage_objects(storage_paths: list[str]) -> None:
    """Best-effort removal of superseded tarball objects after the commit."""
    if not storage_paths:
        return
    file_store = get_file_store()
    for path in storage_paths:
        try:
            await asyncio.to_thread(file_store.delete, path)
        except ObjectNotFoundError:
            pass  # already gone
        except Exception:
            logger.exception("Failed to delete superseded tarball at %s", path)


async def _resolve_tarball_path(
    session: AsyncSession,
    fields: dict,
    deserialized: DeserializedAutomation,
    slug: str,
    existing: Automation | None,
    pending_storage_deletes: list[str],
    owner: _Owner,
) -> str:
    if deserialized.tarball_bytes is not None:
        if existing is not None and await _stored_tarball_matches(
            session, existing, deserialized.tarball_bytes
        ):
            # Nothing changed under tarball/ -- a YAML-only edit (say,
            # flipping `enabled`). Re-uploading would write a second full copy
            # of identical content on every such edit.
            return existing.tarball_path

        new_path = await _write_tarball_upload(
            session,
            owner.user_id,
            owner.org_id,
            deserialized.tarball_bytes,
            slug,
        )
        if existing is not None:
            await _delete_superseded_upload(
                session, existing.tarball_path, pending_storage_deletes
            )
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
    pending_storage_deletes: list[str],
    owner: _Owner,
    existing: Automation | None = None,
) -> dict:
    """Validate automation.yaml fields and resolve a tarball_path, mirroring
    the API request schemas (schemas.py).

    Returns a dict rather than mutating an Automation field by field, so a
    failure partway through can't leave a half-applied automation.
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
        session, fields, deserialized, slug, existing, pending_storage_deletes, owner
    )

    return {
        "name": name,
        "model": fields.get("model"),
        "trigger": trigger.model_dump(),
        "entrypoint": entrypoint,
        "setup_script_path": setup_script_path,
        "timeout": timeout,
        "keep_alive": fields.get("keep_alive"),
        # `dict.get`'s default only applies when the key is absent. A hand edit
        # leaving "enabled:" empty is valid YAML parsing to None, and
        # bool(None) would silently disable a live automation on import.
        "enabled": True if fields.get("enabled") is None else bool(fields["enabled"]),
        "prompt": fields.get("prompt"),
        "preset_metadata": fields.get("preset_metadata"),
        "tarball_path": tarball_path,
    }


async def _create_automation_from_git(
    session: AsyncSession,
    owner: _Owner | None,
    slug: str,
    deserialized: DeserializedAutomation,
    dir_files: dict[str, bytes],
    head: str,
    pending_storage_deletes: list[str],
) -> None:
    if owner is None:
        # A ValueError skips just this directory (see the import loop) rather
        # than aborting the cycle: exports and updates don't need an owner.
        raise ValueError(
            "no user to own automations imported from git; save the Git Sync "
            "configuration once so imports have an owner to run as"
        )
    values = await _validate_and_resolve_fields(
        session, deserialized.fields, deserialized, slug, pending_storage_deletes, owner
    )
    automation = Automation(user_id=owner.user_id, org_id=owner.org_id, **values)
    session.add(automation)
    await session.flush()

    session.add(
        AutomationGitSyncState(
            automation_id=automation.id,
            org_id=owner.org_id,
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
    pending_storage_deletes: list[str],
) -> None:
    new_hash = compute_content_hash(dir_files)
    if new_hash == state.content_hash:
        # No real content change -- usually the commit a prior cycle's export
        # pushed itself.
        state.last_synced_commit = head
        return

    automation = await session.get(Automation, state.automation_id)
    if automation is None:
        raise ValueError(
            f"automation {state.automation_id} not found for slug {state.slug!r}"
        )

    values = await _validate_and_resolve_fields(
        session,
        deserialized.fields,
        deserialized,
        state.slug,
        pending_storage_deletes,
        _Owner(automation.user_id, automation.org_id),
        existing=automation,
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

    `None` means "consider every directory", for when there is no usable base
    to diff from: the first cycle, a shallow clone, or a history rewrite.
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

    Symlinks are excluded: `is_dir()` follows them, so a committed symlink to a
    host directory would be read as an automation directory, and
    `_read_directory_files`' guard never fires for files reached *through* a
    link (they report `is_symlink() == False`). It would also wedge the export,
    since `shutil.rmtree` raises on a symlinked directory.
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
    org_id: uuid.UUID,
    owner: _Owner | None,
    workdir: Path,
    sync_root: Path,
    sync_path: str,
    last_commit: str | None,
    head: str,
    timeout: float,
    encryption_key: str,
    result: SyncCycleResult,
    pending_storage_deletes: list[str],
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

    states = (
        (
            await session.execute(
                select(AutomationGitSyncState).where(
                    AutomationGitSyncState.org_id == org_id
                )
            )
        )
        .scalars()
        .all()
    )
    states_by_slug = {s.slug: s for s in states}

    for slug, directory in dirs_to_process.items():
        state = states_by_slug.get(slug)
        if state is not None and state.dirty:
            # VM wins this cycle; the export overwrites git with the DB's copy.
            continue

        dir_files = await asyncio.to_thread(_read_directory_files, directory)
        # A rolled-back savepoint revives the soft-deleted upload rows queued
        # inside it, so their storage paths must not stay pending for deletion.
        pending_snapshot = len(pending_storage_deletes)
        try:
            # One SAVEPOINT per directory: a failure rolls back just its writes
            # (a half-populated Automation, a flushed TarballUpload) and leaves
            # the session usable for the rest.
            async with session.begin_nested():
                if encryption_key:
                    dir_files = decrypt_file_tree(dir_files, encryption_key)
                deserialized = deserialize_automation(dir_files)
                if deserialized is None:
                    continue

                if state is None:
                    await _create_automation_from_git(
                        session,
                        owner,
                        slug,
                        deserialized,
                        dir_files,
                        head,
                        pending_storage_deletes,
                    )
                    result.imported += 1
                else:
                    await _update_automation_from_git(
                        session,
                        state,
                        deserialized,
                        dir_files,
                        head,
                        pending_storage_deletes,
                    )
        except (ValidationError, ValueError) as e:
            del pending_storage_deletes[pending_snapshot:]
            logger.warning(
                "Skipping invalid automation directory %r from git: %s", slug, e
            )
        except Exception:
            del pending_storage_deletes[pending_snapshot:]
            # Deliberately broad: anything escaping aborts the whole cycle --
            # no export, no push -- every cycle until someone fixes that one
            # directory by hand. Traceback logged, since reaching here means an
            # error shape we didn't anticipate.
            logger.exception(
                "Unexpected error importing automation directory %r from git; "
                "skipping it and continuing the cycle",
                slug,
            )

    # Known automations whose directory disappeared. Checked against `all_dirs`,
    # not the diff-scoped `dirs_to_process`, so an unchanged directory is not
    # mistaken for a deleted one.
    for slug, state in states_by_slug.items():
        if slug in all_dirs or state.dirty:
            continue
        if changed_slugs is not None and slug not in changed_slugs:
            # Missing but untouched by this range: a pre-existing
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
    session: AsyncSession,
    org_id: uuid.UUID,
    workdir: Path,
    old_path: str,
    new_path: str,
) -> None:
    """Re-export every automation after `git_sync_path` changes.

    Nothing about the automations changed, so none are dirty and the export
    would write nothing under the new path -- leaving it empty while the old
    one kept stale copies. Marking them dirty makes the export rewrite them
    all there.

    Also protects the import step, which runs first: a non-dirty state whose
    directory is absent from the still-empty new path would read as an
    automation deleted in git.

    The old directory is left in place -- deleting from the user's repo on a
    config change is not this loop's call -- so it is reported loudly instead.
    """
    await session.execute(
        update(AutomationGitSyncState)
        .where(AutomationGitSyncState.org_id == org_id)
        .values(dirty=True)
    )

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


async def _backfill_missing_states(session: AsyncSession, org_id: uuid.UUID) -> int:
    """Create state rows for the org's live automations that never had one.

    `mark_git_sync_dirty` fires only on an API create/update/delete, so
    automations predating git sync had no state row -- and the export only
    reads dirty rows. The cycle reported success against an empty repo, and an
    automation appeared only if someone edited it again; a restore from that
    repo silently lost the rest.

    New rows start dirty so this cycle exports them. Runs every cycle, so the
    steady state is one indexed NOT EXISTS returning nothing.
    """
    missing = (
        (
            await session.execute(
                select(Automation).where(
                    Automation.org_id == org_id,
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
        (
            await session.execute(
                select(AutomationGitSyncState.slug).where(
                    AutomationGitSyncState.org_id == org_id
                )
            )
        )
        .scalars()
        .all()
    )
    for automation in missing:
        slug = compute_slug(automation.name, automation.id, taken=taken)
        taken.add(slug)
        session.add(
            AutomationGitSyncState(
                automation_id=automation.id, org_id=org_id, slug=slug, dirty=True
            )
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

    Compares decrypted plaintext, not the bytes on disk: Fernet uses a fresh IV
    per encryption, so identical content re-encrypts to different ciphertext
    and every cycle would look changed. Only serializer-owned paths count.
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
        # Unreadable or undecryptable (rotated key, corrupted commit): treat as
        # stale and rewrite rather than skipping the export.
        logger.warning(
            "Could not read the existing export at %s; rewriting it", directory
        )
        return False
    return compute_content_hash(on_disk) == compute_content_hash(files)


async def _export_dirty_automations(
    session: AsyncSession,
    org_id: uuid.UUID,
    sync_root: Path,
    encryption_key: str,
    result: SyncCycleResult,
) -> bool:
    states = (
        (
            await session.execute(
                select(AutomationGitSyncState).where(
                    AutomationGitSyncState.org_id == org_id,
                    AutomationGitSyncState.dirty.is_(True),
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

        # Compared against disk, not `state.content_hash`. The import skips a
        # dirty slug so this export can overwrite git -- but a git-side edit
        # leaves the DB row and its hash untouched, so a hash comparison found
        # nothing to write. The edit was neither imported nor overwritten while
        # `last_commit` advanced past it, and the two sides never reconciled.
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


def _lease_ttl(git_settings: GitSyncSettings) -> timedelta:
    return timedelta(
        seconds=max(
            _SYNC_LEASE_MIN_SECONDS,
            _SYNC_LEASE_TIMEOUT_MULTIPLIER * git_settings.git_sync_git_timeout_seconds,
        )
    )


def lease_is_live(
    started_at: datetime | None,
    git_settings: GitSyncSettings,
    now: datetime | None = None,
) -> bool:
    """Whether an org's `sync_started_at` marks a cycle that is still running.

    The lease lives on the org's row rather than in-process so every replica
    reports the same `sync_in_progress`; a stale one -- a cycle that crashed
    without clearing it -- reads as idle once past the TTL.
    """
    if started_at is None:
        return False
    return (now or utcnow()) - ensure_utc(started_at) < _lease_ttl(git_settings)


async def _acquire_sync_lease(
    session_factory: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    started_at: datetime,
    ttl: timedelta,
) -> bool:
    """Claim the org's lease unless a live cycle holds it.

    One conditional UPDATE, so two replicas -- or the periodic loop on one and
    a manual trigger on another -- racing for the same org can't both win: the
    database serializes the writes and exactly one sees a matching row.
    """
    async with session_factory() as session:
        # Local mode may configure the repo from env alone, with no row yet.
        await get_or_create_org_config(session, org_id)
        result: CursorResult = await session.execute(  # type: ignore[assignment]
            update(AutomationGitSyncOrgConfig)
            .where(
                AutomationGitSyncOrgConfig.org_id == org_id,
                or_(
                    AutomationGitSyncOrgConfig.sync_started_at.is_(None),
                    AutomationGitSyncOrgConfig.sync_started_at < started_at - ttl,
                ),
            )
            .values(sync_started_at=started_at)
            # The database decides the match and `rowcount` reports it. The
            # default also re-evaluates the WHERE in Python against the row
            # loaded above, where SQLite's naive timestamp can't be compared
            # with the aware `started_at` and raised instead of skipping.
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        return result.rowcount == 1


async def _release_sync_lease(
    session_factory: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    started_at: datetime,
) -> None:
    """Clear the lease, but only if it is still ours.

    A cycle that outlived the TTL may have been superseded by another
    replica's; that one's timestamp differs from ours, so it is left alone.
    """
    async with session_factory() as session:
        await session.execute(
            update(AutomationGitSyncOrgConfig)
            .where(
                AutomationGitSyncOrgConfig.org_id == org_id,
                AutomationGitSyncOrgConfig.sync_started_at == started_at,
            )
            .values(sync_started_at=None)
            # As in `_acquire_sync_lease`: nothing in this session is read
            # afterwards, so the database alone decides the match.
            .execution_options(synchronize_session=False)
        )
        await session.commit()


async def run_sync_cycle(
    session_factory: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    git_settings: GitSyncSettings,
    service_settings: ServiceSettings,
) -> SyncCycleResult:
    """Run one full pull -> import -> export -> push cycle for one org.

    Takes the org's lease first. Replicas share the database but not a
    process, so an in-process lock can't keep two pods -- or the periodic loop
    on one and a manual trigger on another -- from cloning and pushing the
    same repo at once. `skipped` means another cycle holds the lease; it picks
    up everything this one would have.
    """
    started_at = utcnow()
    if not await _acquire_sync_lease(
        session_factory, org_id, started_at, _lease_ttl(git_settings)
    ):
        return SyncCycleResult(head=None, skipped=True)
    try:
        return await _run_sync_cycle_leased(
            session_factory, org_id, git_settings, service_settings
        )
    except Exception as e:
        async with session_factory() as session:
            org_config = await get_or_create_org_config(session, org_id)
            org_config.last_error = str(e)[:2000]
            org_config.last_error_at = utcnow()
            await session.commit()
        raise
    finally:
        await _release_sync_lease(session_factory, org_id, started_at)


async def _run_sync_cycle_leased(
    session_factory: async_sessionmaker[AsyncSession],
    org_id: uuid.UUID,
    git_settings: GitSyncSettings,
    service_settings: ServiceSettings,
) -> SyncCycleResult:
    workdir = _resolve_workdir(git_settings, service_settings, org_id)
    timeout = git_settings.git_sync_git_timeout_seconds
    token = git_settings.git_sync_token
    branch = git_settings.git_sync_branch.strip()
    encryption_key = git_settings.git_sync_encryption_key

    # Re-validated here, not only at the API boundary, since this also covers a
    # misconfigured AUTOMATION_GIT_SYNC_PATH and `sync_root` is what the export
    # rmtree's per automation. Raising leaves the reason in the org's
    # `last_error` instead of deleting host directories that happen to share a
    # name with an automation slug.
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

    # Storage paths of superseded uploads soft-deleted during the import; their
    # objects are only removed after the commit below succeeds, so a rollback
    # can never revive a record whose object is already gone.
    pending_storage_deletes: list[str] = []

    # Committed *before* the push, not held open across it: SQLite holds its
    # single-writer lock for the whole transaction, and a push can take up to
    # git_sync_git_timeout_seconds.
    async with session_factory() as session:
        org_config = await get_or_create_org_config(session, org_id)
        last_commit = org_config.last_synced_commit
        owner = _import_owner(org_config)

        # Before the import: a path change leaves the new sync_root empty, and
        # the import's "directory disappeared" branch would read that as every
        # automation having been deleted in git.
        last_path = org_config.last_synced_path
        if last_path is not None and last_path != sync_path:
            await _handle_path_change(session, org_id, workdir, last_path, sync_path)
        org_config.last_synced_path = sync_path

        if head is not None and head != last_commit:
            await _import_from_git(
                session,
                org_id,
                owner,
                workdir,
                sync_root,
                sync_path,
                last_commit,
                head,
                timeout,
                encryption_key,
                result,
                pending_storage_deletes,
            )

        # After the import, so automations that arrived from git this cycle
        # already have their state row and aren't re-created here.
        await _backfill_missing_states(session, org_id)

        # Return value unused: the counters land on `result`, and the push must
        # run regardless of whether anything was exported (see below).
        await _export_dirty_automations(
            session, org_id, sync_root, encryption_key, result
        )
        await session.commit()

    # Only after the commit: the soft-deletes are durable, so removing the
    # objects can no longer strand a live record. A commit failure skips this.
    await _delete_pending_storage_objects(pending_storage_deletes)

    # Unconditional, not gated on `exported`: commit_and_push also retries a
    # previous cycle's unpushed commit and pushes to a newly-repointed remote.
    # Gating made those recovery paths unreachable whenever there was nothing
    # new to export, so the cycle reported success while never pushing. It
    # self-limits -- with nothing staged or pending it returns None untouched.
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
        org_config = await get_or_create_org_config(session, org_id)
        if new_head:
            org_config.last_synced_commit = new_head
        org_config.last_run_at = utcnow()
        # Both halves, or the status endpoint reports a `last_error_at` with no
        # `last_error` to go with it.
        org_config.last_error = None
        org_config.last_error_at = None
        await session.commit()

    return result


def _is_due(
    org_config: AutomationGitSyncOrgConfig, interval_seconds: int, now: datetime
) -> bool:
    """Whether the org's interval has elapsed since its last cycle ended.

    A failed cycle counts as an attempt too: a repo that is down waits out the
    interval like a healthy one instead of being retried every tick.
    """
    attempts = [
        ensure_utc(at)
        for at in (org_config.last_run_at, org_config.last_error_at)
        if at is not None
    ]
    if not attempts:
        return True
    return now - max(attempts) >= timedelta(seconds=interval_seconds)


async def git_sync_loop(
    session_factory: async_sessionmaker[AsyncSession],
    shutdown_event: asyncio.Event | None = None,
) -> None:
    """Background loop: periodically syncs each configured org with its repo.

    Started unconditionally by app.py. Every tick re-reads each org's runtime
    config and runs a cycle for the orgs that are enabled, have a positive
    interval and are due; the rest idle without dying. In local mode the one
    local org may be configured from env alone, so its row is created here.

    Ticks every `_IDLE_POLL_SECONDS` instead of sleeping an org's interval:
    intervals are per org and set from the UI, and a short tick is what lets a
    newly-set one take effect without a restart. An org never syncs while its
    interval is 0 (manual-only).
    """
    service_settings = get_config().service

    logger.info(
        "Git sync loop started; each org's interval is set from the UI, "
        "0 means manual-only"
    )

    async def _next_interval() -> float:
        return float(_IDLE_POLL_SECONDS)

    async def _cycle() -> None:
        now = utcnow()
        due: list[tuple[uuid.UUID, GitSyncSettings]] = []
        async with session_factory() as session:
            if service_settings.is_local_mode:
                await get_or_create_org_config(session, _get_local_user().org_id)
                await session.commit()
            rows = (
                (await session.execute(select(AutomationGitSyncOrgConfig)))
                .scalars()
                .all()
            )
            for org_config in rows:
                effective = effective_settings_for(org_config)
                interval = effective_interval_for(org_config)
                if not effective.enabled:
                    logger.debug(
                        "Git sync for org %s is paused or unconfigured; skipping",
                        org_config.org_id,
                    )
                    continue
                if interval <= 0:
                    logger.debug(
                        "Git sync for org %s is manual-only; skipping this "
                        "automatic cycle",
                        org_config.org_id,
                    )
                    continue
                if _is_due(org_config, interval, now):
                    due.append((org_config.org_id, effective))

        # One org's failure must not stop the others, so each gets its own
        # try: `run_periodic_loop` only guards the tick as a whole.
        for org_id, effective in due:
            try:
                result = await run_sync_cycle(
                    session_factory, org_id, effective, service_settings
                )
            except GitSyncError:
                logger.exception("Git sync cycle failed for org %s", org_id)
                continue
            except Exception:
                logger.exception(
                    "Unexpected error in git sync cycle for org %s", org_id
                )
                continue
            if result.skipped:
                logger.debug(
                    "Git sync cycle for org %s skipped: another cycle holds the lease",
                    org_id,
                )
            elif any(
                (
                    result.imported,
                    result.exported,
                    result.deleted_in_db,
                    result.deleted_in_git,
                )
            ):
                logger.info(
                    "Git sync cycle complete for org %s: imported=%d exported=%d "
                    "deleted_in_db=%d deleted_in_git=%d pushed=%s",
                    org_id,
                    result.imported,
                    result.exported,
                    result.deleted_in_db,
                    result.deleted_in_git,
                    result.pushed_commit,
                )

    await run_periodic_loop(
        _cycle,
        interval_seconds=_next_interval,
        shutdown_event=shutdown_event,
        logger=logger,
        name="Git sync",
    )
