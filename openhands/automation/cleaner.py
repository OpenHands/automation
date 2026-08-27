"""Periodic workspace purging for local-mode terminal runs.

Purging is independent from database-row retention. It only removes
filesystem workspace directories; database rows are managed separately.
"""

import asyncio
import logging
import os
import shutil
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from itertools import batched
from pathlib import Path
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from openhands.automation.backends.local import resolve_local_workspace_base
from openhands.automation.models import AutomationRun, AutomationRunStatus
from openhands.automation.utils import ensure_utc, utcnow


logger = logging.getLogger("automation.cleaner")

# 500 stays below SQLite's historical 999-variable default as well as asyncpg's
# much larger parameter ceiling, leaving room for future query predicates.
DB_CLASSIFICATION_CHUNK_SIZE: Final[int] = 500
DELETE_ATTEMPT_FACTOR: Final[int] = 3


@dataclass(frozen=True, slots=True)
class PurgeResult:
    """Result of a workspace purge run."""

    candidates_found: int
    deleted: int
    missing: int
    refused: int
    errors: int
    bytes_freed: int


class DeleteOutcome(StrEnum):
    """Outcome of one bounded workspace deletion attempt."""

    DELETED = "deleted"
    MISSING = "missing"
    REFUSED = "refused"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class WorkspaceDeleteResult:
    outcome: DeleteOutcome
    bytes_freed: int


def _empty_result(candidates_found: int = 0) -> PurgeResult:
    return PurgeResult(candidates_found, 0, 0, 0, 0, 0)


def _workspace_root(workspace_base: str | os.PathLike[str] | None) -> Path:
    """Return the normalized root that owns all automation run directories."""
    base = resolve_local_workspace_base(workspace_base)
    return Path(base).resolve(strict=False) / "automation-runs"


def _workspace_path(
    workspace_base: str | os.PathLike[str] | None, run_id: UUID
) -> Path:
    """Build a run path from a typed UUID, never from raw path input."""
    return _workspace_root(workspace_base) / str(run_id)


def _scan_candidates(runs_root: Path) -> dict[UUID, float]:
    """Find direct, canonical UUID workspace directories under ``runs_root``."""
    if _is_link_or_junction(runs_root):
        logger.warning("Refusing linked workspace root during scan: %s", runs_root)
        return {}

    try:
        if not runs_root.is_dir():
            return {}
        resolved_root = runs_root.resolve(strict=True)
        candidates: list[tuple[UUID, float]] = []
        with os.scandir(runs_root) as entries:
            for entry in entries:
                with suppress(OSError, ValueError):
                    candidate_path = Path(entry.path)
                    if entry.is_symlink() or _is_link_or_junction(candidate_path):
                        continue
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    run_id = UUID(entry.name)
                    if str(run_id) != entry.name:
                        continue
                    resolved_path = candidate_path.resolve(strict=True)
                    if (
                        resolved_path.parent != resolved_root
                        or not resolved_path.is_dir()
                    ):
                        continue
                    mtime = entry.stat(follow_symlinks=False).st_mtime
                    candidates.append((run_id, mtime))
    except FileNotFoundError:
        return {}
    except OSError as exc:
        logger.warning("Failed to scan workspace root %s: %s", runs_root, exc)
        return {}

    candidates.sort(key=lambda item: (item[1], str(item[0])))
    return dict(candidates)


def _dir_size(path: Path) -> int:
    """Best-effort reclaimed-space estimate; may undercount on races."""
    total = 0
    with suppress(OSError):
        for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
            # Unlike symlinks, os.walk descends Windows junctions even with
            # followlinks=False; prune them so bytes rmtree would never delete
            # are not reported as reclaimed.
            dirnames[:] = [
                name
                for name in dirnames
                if not _is_link_or_junction(Path(dirpath) / name)
            ]
            for filename in filenames:
                # stat(follow_symlinks=False) reads the link itself rather than
                # the target, so external targets never count as workspace bytes.
                with suppress(OSError):
                    total += (
                        (Path(dirpath) / filename).stat(follow_symlinks=False).st_size
                    )
    return total


def _is_link_or_junction(path: Path) -> bool:
    """Return whether path is a symlink or Windows directory junction."""
    return path.is_symlink() or path.is_junction()


def _is_expired(timestamp: datetime | float, cutoff: datetime) -> bool:
    """Return whether a trusted timestamp is older than ``cutoff``."""
    try:
        if isinstance(timestamp, datetime):
            return ensure_utc(timestamp) < cutoff
        return datetime.fromtimestamp(timestamp, tz=UTC) < cutoff
    except (OverflowError, OSError, ValueError):
        return False


def _is_terminal(status: AutomationRunStatus) -> bool:
    return status in {
        AutomationRunStatus.COMPLETED,
        AutomationRunStatus.FAILED,
        AutomationRunStatus.CANCELLED,
        AutomationRunStatus.SKIPPED,
    }


def _delete_workspace(
    workspace_base: str | os.PathLike[str] | None,
    run_id: UUID,
) -> WorkspaceDeleteResult:
    """Delete one verified run directory without following root reparse points."""
    runs_root = _workspace_root(workspace_base)
    workspace_path = _workspace_path(workspace_base, run_id)

    if _is_link_or_junction(runs_root):
        logger.warning("Refusing linked workspace root: %s", runs_root)
        return WorkspaceDeleteResult(DeleteOutcome.REFUSED, 0)
    if _is_link_or_junction(workspace_path):
        logger.warning("Refusing linked workspace path: %s", workspace_path)
        return WorkspaceDeleteResult(DeleteOutcome.REFUSED, 0)
    if not workspace_path.exists():
        return WorkspaceDeleteResult(DeleteOutcome.MISSING, 0)

    try:
        resolved_root = runs_root.resolve(strict=True)
        resolved_path = workspace_path.resolve(strict=True)
    except FileNotFoundError:
        return WorkspaceDeleteResult(DeleteOutcome.MISSING, 0)
    except OSError as exc:
        logger.warning("Failed to resolve workspace %s: %s", workspace_path, exc)
        return WorkspaceDeleteResult(DeleteOutcome.ERROR, 0)

    if resolved_path.parent != resolved_root or not resolved_path.is_dir():
        logger.warning("Refusing workspace outside expected root: %s", workspace_path)
        return WorkspaceDeleteResult(DeleteOutcome.REFUSED, 0)

    size = _dir_size(workspace_path)
    try:
        shutil.rmtree(workspace_path)
        return WorkspaceDeleteResult(DeleteOutcome.DELETED, size)
    except FileNotFoundError:
        return WorkspaceDeleteResult(DeleteOutcome.MISSING, 0)
    except OSError as exc:
        logger.warning("Failed to delete workspace %s: %s", workspace_path, exc)
        return WorkspaceDeleteResult(DeleteOutcome.ERROR, 0)


async def purge_terminal_workspaces(
    session_factory: async_sessionmaker[AsyncSession],
    workspace_base: str | os.PathLike[str] | None,
    retention_seconds: int,
    batch_size: int = 50,
    shutdown_event: asyncio.Event | None = None,
) -> PurgeResult:
    """Purge expired terminal and inactive orphan workspace directories.

    ``batch_size`` bounds successful deletions per cycle. Total deletion
    attempts are additionally capped at ``DELETE_ATTEMPT_FACTOR`` times that
    value, so permanently failing candidates cannot produce an unbounded
    warning storm in a single cycle. This caps failure cost per cycle only;
    a candidate that fails every cycle can still consume all attempts, so no
    eventual-progress (anti-starvation) guarantee is implied.
    A retention of zero disables purging.
    """
    if retention_seconds < 0:
        raise ValueError("retention_seconds must be non-negative")
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if retention_seconds == 0 or (
        shutdown_event is not None and shutdown_event.is_set()
    ):
        return _empty_result()

    cutoff = utcnow() - timedelta(seconds=retention_seconds)
    candidates = await asyncio.to_thread(
        _scan_candidates, _workspace_root(workspace_base)
    )
    if shutdown_event is not None and shutdown_event.is_set():
        return _empty_result(len(candidates))

    by_id: dict[UUID, tuple[AutomationRunStatus, datetime | None]] = {}
    if candidates:
        async with session_factory() as session:
            for candidate_chunk in batched(candidates, DB_CLASSIFICATION_CHUNK_SIZE):
                stmt = select(
                    AutomationRun.id,
                    AutomationRun.status,
                    AutomationRun.completed_at,
                ).where(AutomationRun.id.in_(candidate_chunk))
                rows = (await session.execute(stmt)).all()
                by_id.update(
                    (run_id, (status, completed_at))
                    for run_id, status, completed_at in rows
                )

    deleted = missing = refused = errors = bytes_freed = attempts = 0
    max_attempts = batch_size * DELETE_ATTEMPT_FACTOR
    for run_id, root_mtime in candidates.items():
        if deleted >= batch_size or attempts >= max_attempts:
            break
        if shutdown_event is not None and shutdown_event.is_set():
            break

        row = by_id.get(run_id)
        if row is not None:
            status, completed_at = row
            if not _is_terminal(status):
                continue
        else:
            completed_at = None

        # Service-owned live runs retain an AutomationRun row: run directories
        # are only designated for dispatched runs, and dispatch requires a DB
        # row. No separate authoritative live-workspace registry exists, so for
        # orphans (and terminal rows without completed_at) the scandir-captured
        # mtime against the retention window is the guard — the maintainer's
        # chosen design, which also bounds any out-of-band DB reset/repoint
        # exposure to workspaces idle past the full retention window.
        age = completed_at if completed_at is not None else root_mtime
        if not _is_expired(age, cutoff):
            continue
        if shutdown_event is not None and shutdown_event.is_set():
            break

        attempts += 1
        delete_result = await asyncio.to_thread(
            _delete_workspace, workspace_base, run_id
        )
        match delete_result.outcome:
            case DeleteOutcome.MISSING:
                missing += 1
            case DeleteOutcome.REFUSED:
                refused += 1
            case DeleteOutcome.ERROR:
                errors += 1
            case DeleteOutcome.DELETED:
                deleted += 1
                bytes_freed += delete_result.bytes_freed
                logger.info(
                    "Purged workspace for run %s (%d bytes freed)",
                    run_id,
                    delete_result.bytes_freed,
                )

    result = PurgeResult(
        len(candidates), deleted, missing, refused, errors, bytes_freed
    )
    # An idle cycle must not flood INFO every interval; per-candidate failures
    # are already logged as warnings inside _delete_workspace.
    if result.deleted:
        logger.info(
            "Purge complete: %d deleted, %d missing, %d refused, %d errors, "
            "%d bytes freed (%d candidates scanned)",
            result.deleted,
            result.missing,
            result.refused,
            result.errors,
            result.bytes_freed,
            result.candidates_found,
        )
    else:
        logger.debug(
            "Purge no-op: %d missing, %d refused, %d errors (%d candidates scanned)",
            result.missing,
            result.refused,
            result.errors,
            result.candidates_found,
        )
    return result


async def purger_loop(
    session_factory: async_sessionmaker[AsyncSession],
    workspace_base: str | os.PathLike[str] | None,
    retention_seconds: int,
    interval_seconds: int,
    batch_size: int = 50,
    shutdown_event: asyncio.Event | None = None,
) -> None:
    """Periodically purge workspaces until shutdown; zero retention disables it."""
    if retention_seconds < 0:
        raise ValueError("retention_seconds must be non-negative")
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    if retention_seconds == 0:
        logger.info("Workspace purger disabled: retention is 0")
        return

    logger.info(
        "Workspace purger started: retention=%ds interval=%ds batch=%d",
        retention_seconds,
        interval_seconds,
        batch_size,
    )
    while shutdown_event is None or not shutdown_event.is_set():
        try:
            await purge_terminal_workspaces(
                session_factory=session_factory,
                workspace_base=workspace_base,
                retention_seconds=retention_seconds,
                batch_size=batch_size,
                shutdown_event=shutdown_event,
            )
        except Exception:
            logger.exception("Error in workspace purge scan")

        if shutdown_event is None:
            await asyncio.sleep(interval_seconds)
            continue
        with suppress(TimeoutError):
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval_seconds)

    logger.info("Workspace purger received shutdown signal, exiting")
