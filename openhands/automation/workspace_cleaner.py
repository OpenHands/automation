"""Periodic workspace purging for local-mode terminal runs.

Purging is independent from database-row retention. It only removes
filesystem workspace directories; database rows are managed separately.
"""

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from openhands.automation.models import AutomationRun, AutomationRunStatus


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
    errors: int = 0
    bytes_freed: int = 0


def _workspace_path(workspace_base: str, run_id: str) -> str:
    return os.path.join(workspace_base, "automation-runs", run_id)


def _dir_size(path: str) -> int:
    total = 0
    try:
        for dirpath, _dirnames, filenames in os.walk(path):
            for filename in filenames:
                filepath = os.path.join(dirpath, filename)
                try:
                    total += os.path.getsize(filepath)
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _delete_workspace(workspace_path: str) -> int | None:
    """Delete a workspace directory. Returns bytes freed or None on error."""
    try:
        size = _dir_size(workspace_path)
    except OSError:
        size = 0

    try:
        shutil.rmtree(workspace_path)
        return size
    except FileNotFoundError:
        return 0
    except OSError as e:
        logger.warning("Failed to delete workspace %s: %s", workspace_path, e)
        return None


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
        batch_size: Maximum number of workspaces to purge per call.

    Returns:
        PurgeResult with counts of candidates, deletions, errors, and bytes freed.
    """
    cutoff = datetime.utcnow() - timedelta(seconds=retention_seconds)
    result = PurgeResult()

    async with session_factory() as session:
        stmt = (
            select(AutomationRun.id)
            .where(
                AutomationRun.status.in_(TERMINAL_STATES),
                AutomationRun.completed_at.isnot(None),
                AutomationRun.completed_at < cutoff,
            )
            .order_by(AutomationRun.completed_at.asc())
            .limit(batch_size)
        )
        rows = await session.execute(stmt)
        candidate_ids = [str(row[0]) for row in rows.fetchall()]

    result.candidates_found = len(candidate_ids)
    if not candidate_ids:
        return result

    logger.info("Found %d candidate workspaces for purge", result.candidates_found)

    for run_id in candidate_ids:
        workspace_path = _workspace_path(workspace_base, run_id)
        bytes_freed = _delete_workspace(workspace_path)
        if bytes_freed is not None:
            result.deleted += 1
            result.bytes_freed += bytes_freed
            logger.debug(
                "Purged workspace %s (%d bytes freed)", workspace_path, bytes_freed
            )
        else:
            result.errors += 1

    logger.info(
        "Purge complete: %d deleted, %d errors, %d bytes freed",
        result.deleted,
        result.errors,
        result.bytes_freed,
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
            if result.deleted > 0 or result.errors > 0:
                logger.info(
                    "Purge cycle: %d deleted (%d bytes), %d errors, %d candidates",
                    result.deleted,
                    result.bytes_freed,
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
