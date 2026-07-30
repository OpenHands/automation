"""Periodic workspace purging for local-mode terminal runs.

Purging is independent from database-row retention. It only removes
filesystem workspace directories; database rows are managed separately.
"""

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from openhands.automation.models import AutomationRun, AutomationRunStatus
from openhands.automation.utils import utcnow


logger = logging.getLogger("automation.workspace_cleaner")

TERMINAL_STATES = frozenset(
    {
        AutomationRunStatus.COMPLETED,
        AutomationRunStatus.FAILED,
        AutomationRunStatus.CANCELLED,
        AutomationRunStatus.SKIPPED,
    }
)


@dataclass
class PurgeResult:
    """Result of a workspace purge run."""

    candidates_found: int = 0
    deleted: int = 0
    missing: int = 0
    refused: int = 0
    errors: int = 0
    bytes_freed: int = 0


class DeleteOutcome(Enum):
    """Outcome of one bounded workspace deletion attempt."""

    DELETED = "deleted"
    MISSING = "missing"
    REFUSED = "refused"
    ERROR = "error"


@dataclass(frozen=True)
class WorkspaceDeleteResult:
    outcome: DeleteOutcome
    bytes_freed: int = 0


def _workspace_root(workspace_base: str | Path) -> Path:
    """Return the normalized root that owns all automation run directories.

    Resolve the configured base, but deliberately do not resolve the
    ``automation-runs`` child. The deletion guard must still be able to detect
    if that child has been replaced with a symlink or Windows junction.
    """
    return Path(workspace_base).expanduser().resolve(strict=False) / "automation-runs"


def _workspace_path(workspace_base: str | Path, run_id: UUID) -> Path:
    """Build a run path from a typed database UUID, never from raw path input."""
    return _workspace_root(workspace_base) / str(run_id)


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for dirpath, _dirnames, filenames in os.walk(path):
            for filename in filenames:
                filepath = Path(dirpath) / filename
                try:
                    total += os.path.getsize(filepath)
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _is_link_or_junction(path: Path) -> bool:
    """Return whether path is a symlink or Windows directory junction."""
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or (is_junction is not None and is_junction())


def _delete_workspace(
    workspace_base: str | Path,
    run_id: UUID,
) -> WorkspaceDeleteResult:
    """Delete one verified run directory without following root reparse points."""
    runs_root = _workspace_root(workspace_base)
    workspace_path = _workspace_path(workspace_base, run_id)

    if _is_link_or_junction(runs_root):
        logger.warning("Refusing linked workspace root: %s", runs_root)
        return WorkspaceDeleteResult(DeleteOutcome.REFUSED)

    if _is_link_or_junction(workspace_path):
        logger.warning("Refusing linked workspace path: %s", workspace_path)
        return WorkspaceDeleteResult(DeleteOutcome.REFUSED)

    if not workspace_path.exists():
        return WorkspaceDeleteResult(DeleteOutcome.MISSING)

    try:
        resolved_root = runs_root.resolve(strict=True)
        resolved_path = workspace_path.resolve(strict=True)
    except FileNotFoundError:
        return WorkspaceDeleteResult(DeleteOutcome.MISSING)
    except OSError as exc:
        logger.warning("Failed to resolve workspace %s: %s", workspace_path, exc)
        return WorkspaceDeleteResult(DeleteOutcome.ERROR)

    if resolved_path.parent != resolved_root or not resolved_path.is_dir():
        logger.warning("Refusing workspace outside expected root: %s", workspace_path)
        return WorkspaceDeleteResult(DeleteOutcome.REFUSED)

    size = _dir_size(workspace_path)
    try:
        shutil.rmtree(workspace_path)
        return WorkspaceDeleteResult(DeleteOutcome.DELETED, size)
    except FileNotFoundError:
        return WorkspaceDeleteResult(DeleteOutcome.MISSING)
    except OSError as exc:
        logger.warning("Failed to delete workspace %s: %s", workspace_path, exc)
        return WorkspaceDeleteResult(DeleteOutcome.ERROR)


async def purge_terminal_workspaces(
    session_factory: async_sessionmaker[AsyncSession],
    workspace_base: str,
    retention_seconds: int,
    batch_size: int = 50,
) -> PurgeResult:
    """Purge workspace directories for terminal runs past the retention period.

    Only removes filesystem directories; database rows are not touched.

    Never removes workspaces for pending or running runs. Only runs in a
    terminal state (COMPLETED, FAILED, CANCELLED, SKIPPED) with a
    ``completed_at`` older than the retention cutoff are eligible.

    Args:
        session_factory: Factory for async database sessions.
        workspace_base: Expanded base directory for workspaces.
        retention_seconds: Minimum age in seconds before a workspace is purged.
        batch_size: Maximum number of existing workspaces to delete or attempt per
            call. Missing directories do not consume the limit, preventing old
            retained database rows from starving later cleanup candidates.

    Returns:
        PurgeResult with counts of candidates, deletions, errors, and bytes freed.
    """
    if retention_seconds < 0:
        raise ValueError("retention_seconds must be non-negative")
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")

    cutoff = utcnow() - timedelta(seconds=retention_seconds)
    result = PurgeResult()

    async with session_factory() as session:
        stmt = (
            select(AutomationRun.id)
            .where(
                AutomationRun.status.in_(TERMINAL_STATES),
                AutomationRun.completed_at.isnot(None),
                AutomationRun.completed_at < cutoff,
            )
            .order_by(AutomationRun.completed_at.asc(), AutomationRun.id.asc())
            .execution_options(yield_per=batch_size)
        )
        candidate_ids = await session.stream_scalars(stmt)

        async for run_id in candidate_ids:
            result.candidates_found += 1
            delete_result = await asyncio.to_thread(
                _delete_workspace, workspace_base, run_id
            )

            if delete_result.outcome is DeleteOutcome.MISSING:
                # Missing workspaces are expected after manual cleanup. Continue
                # scanning so old missing rows cannot starve later real directories.
                result.missing += 1
                continue
            if delete_result.outcome is DeleteOutcome.REFUSED:
                result.refused += 1
            elif delete_result.outcome is DeleteOutcome.ERROR:
                result.errors += 1
            else:
                result.deleted += 1
                result.bytes_freed += delete_result.bytes_freed
                logger.debug(
                    "Purged workspace for run %s (%d bytes freed)",
                    run_id,
                    delete_result.bytes_freed,
                )

            attempted_existing = result.deleted + result.refused + result.errors
            if attempted_existing >= batch_size:
                break

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
    return result


async def purger_loop(
    session_factory: async_sessionmaker[AsyncSession],
    workspace_base: str,
    retention_seconds: int,
    interval_seconds: int,
    batch_size: int = 50,
    shutdown_event: asyncio.Event | None = None,
) -> None:
    """Periodic loop that purges old terminal-run workspace directories.

    Only runs in local mode. The caller should guard against running this
    in cloud mode.

    Args:
        session_factory: Factory for async database sessions.
        workspace_base: Expanded base directory for workspaces.
        retention_seconds: Minimum age before a workspace is purged.
        interval_seconds: Seconds between purge cycles.
        batch_size: Maximum workspaces purged per cycle.
        shutdown_event: Optional event to signal graceful shutdown.
    """
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")

    logger.info(
        "Workspace purger started: retention=%ds interval=%ds batch=%d",
        retention_seconds,
        interval_seconds,
        batch_size,
    )

    while True:
        if shutdown_event is not None and shutdown_event.is_set():
            logger.info("Workspace purger received shutdown signal, exiting")
            break

        try:
            result = await purge_terminal_workspaces(
                session_factory=session_factory,
                workspace_base=workspace_base,
                retention_seconds=retention_seconds,
                batch_size=batch_size,
            )
            if result.deleted > 0 or result.errors > 0 or result.refused > 0:
                logger.info(
                    "Purge cycle: %d deleted (%d bytes), %d missing, "
                    "%d refused, %d errors, %d candidates",
                    result.deleted,
                    result.bytes_freed,
                    result.missing,
                    result.refused,
                    result.errors,
                    result.candidates_found,
                )
        except Exception:
            logger.exception("Error in workspace purge scan")

        if shutdown_event is not None:
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=interval_seconds)
                logger.info("Workspace purger received shutdown signal, exiting")
                break
            except TimeoutError:
                pass
        else:
            await asyncio.sleep(interval_seconds)
