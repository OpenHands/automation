"""Staleness watchdog for stuck RUNNING automation runs.

Periodically scans for runs stuck in RUNNING state past their pre-computed
``timeout_at`` deadline. Before marking as FAILED, attempts to verify the
actual run status by querying the execution environment. A verification
result that means "the bash command may still be executing" defers the
deadline (bounded by a hard cap) instead of terminalizing the run.

The ``timeout_at`` column is set to a provisioning-phase deadline when the
dispatcher transitions a run to RUNNING (see ``mark_run_status``), then
reset to bash-start + run budget + margin once the bash command starts
(see ``update_run_timeout_at``).

The watchdog is mode-agnostic — all mode-specific logic is encapsulated
in the ExecutionBackend (see automation/backends/).

The same loop prunes ``integration_events`` and local-mode run workspaces: it
is the service's only periodic janitor, and a second loop buys nothing for one
bounded DELETE.
"""

import asyncio
import logging
import os
import shutil
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Final
from uuid import UUID

from sqlalchemy import delete, inspect, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from openhands.automation.backends import get_backend
from openhands.automation.backends.local import local_runs_root
from openhands.automation.config import Settings, get_config
from openhands.automation.models import (
    Automation,
    AutomationRun,
    AutomationRunStatus,
    IntegrationEvent,
)
from openhands.automation.telemetry import capture_automation_event
from openhands.automation.utils import log_extra
from openhands.automation.utils.agent_server import VerificationOutcome
from openhands.automation.utils.run import record_first_run_outcome
from openhands.automation.utils.run_status_detail import (
    RunStatusDetailKind,
    RunStatusPhase,
    make_run_status_detail,
    run_status_detail_from_exception,
    run_status_detail_from_transient_error,
)
from openhands.automation.utils.time import ensure_utc, utcnow
from openhands.automation.utils.timeout import resolve_automation_timeout_seconds


logger = logging.getLogger("automation.watchdog")

# Most rows a single prune deletes. Caps how long one statement holds locks on
# a table the receive path writes to; the loop runs again a minute later, so a
# backlog drains rather than being dropped.
PRUNE_BATCH_SIZE = 5000
WORKSPACE_PURGE_BATCH_SIZE: Final[int] = 50
DELETE_ATTEMPT_FACTOR: Final[int] = 3


async def _get_automation_keep_alive(
    session: AsyncSession, run: AutomationRun
) -> bool | None:
    """Return parent automation keep_alive without extra SQL when preloaded."""
    if "automation" not in inspect(run).unloaded and run.automation is not None:
        return run.automation.keep_alive

    return await session.scalar(
        select(Automation.keep_alive).where(Automation.id == run.automation_id)
    )


async def _get_automation_timeout(
    session: AsyncSession, run: AutomationRun
) -> int | None:
    """Return parent automation timeout without extra SQL when preloaded."""
    if "automation" not in inspect(run).unloaded and run.automation is not None:
        return run.automation.timeout

    return await session.scalar(
        select(Automation.timeout).where(Automation.id == run.automation_id)
    )


async def _defer_still_running(
    session: AsyncSession,
    run: AutomationRun,
    settings: Settings,
    now: datetime,
) -> bool:
    """Push timeout_at forward while the bash command may still be running.

    The bash command's own timeout (enforced by the agent-server from bash
    start) governs how long the run may execute; a still-running verification
    result at the watchdog deadline means only that the two clocks are
    skewed, not that the run failed. Deferral is bounded by a hard cap so a
    broken bash service cannot defer forever.

    Returns True when the run should be left non-terminal this scan
    (deferred, or already terminal via a racing callback/cancel). Returns
    False when the hard cap is exhausted and the caller must proceed to the
    terminal path.
    """
    extra = log_extra(run_id=str(run.id), sandbox_id=run.sandbox_id)
    sandbox_cfg = get_config().sandbox
    effective_timeout = resolve_automation_timeout_seconds(
        await _get_automation_timeout(session, run)
    )
    anchor = ensure_utc(run.started_at or run.created_at)
    hard_deadline = anchor + timedelta(
        seconds=sandbox_cfg.sandbox_ready_timeout
        + effective_timeout
        + sandbox_cfg.run_timeout_hard_grace
    )
    if now >= hard_deadline:
        return False

    new_timeout_at = min(
        now + timedelta(seconds=settings.watchdog_interval_seconds), hard_deadline
    )
    result: CursorResult = await session.execute(  # type: ignore[assignment]
        update(AutomationRun)
        .where(
            AutomationRun.id == run.id,
            AutomationRun.status == AutomationRunStatus.RUNNING,
        )
        .values(timeout_at=new_timeout_at, status_detail=None)
    )
    if result.rowcount > 0:
        logger.info(
            "Run still executing; deferring timeout_at to %s (hard cap %s)",
            new_timeout_at,
            hard_deadline,
            extra=extra,
        )
    # rowcount == 0 means a callback/cancel landed concurrently — also
    # non-terminal for the watchdog.
    return True


def _loaded_automation(run: AutomationRun) -> Automation | None:
    """Return the parent automation only when it is already loaded."""
    if "automation" in inspect(run).unloaded:
        return None
    return run.automation


def _should_cleanup_sandbox_after_terminal(
    run: AutomationRun, keep_alive: bool | None
) -> bool:
    """Return whether watchdog should explicitly delete this run's sandbox."""
    return bool(run.sandbox_id) and keep_alive is not True


async def _verify_and_mark_run(
    session: AsyncSession,
    run: AutomationRun,
    settings: Settings,
) -> bool:
    """Verify run status via backend and mark accordingly.

    Mode-agnostic: all verification logic is encapsulated in the backend.

    Returns True if the run was marked with a terminal status.
    """
    run_id = str(run.id)
    sandbox_id = run.sandbox_id
    extra = log_extra(run_id=run_id, sandbox_id=sandbox_id)
    now = utcnow()

    # Get backend for this run (mode-specific logic encapsulated)
    backend = get_backend(run)

    # Verify run status via backend
    try:
        logger.info("Verifying run status via backend", extra=extra)
        verification = await backend.verify_run(run_id)
    except Exception as e:
        logger.warning("Failed to verify run: %s", e, extra=extra)
        stmt = (
            update(AutomationRun)
            .where(
                AutomationRun.id == run.id,
                AutomationRun.status == AutomationRunStatus.RUNNING,
            )
            .values(
                status=AutomationRunStatus.FAILED,
                completed_at=now,
                error_detail=f"Timed out: verification failed: {e}",
                status_detail=run_status_detail_from_exception(
                    e,
                    phase=RunStatusPhase.VERIFICATION,
                    source="automation_service",
                    operation="verify_run",
                    previous=run.status_detail,
                ),
            )
        )
        result: CursorResult = await session.execute(stmt)  # type: ignore[assignment]
        if result.rowcount > 0:
            await capture_automation_event(
                "automation_run_failed",
                automation=_loaded_automation(run),
                run=run,
                session=session,
                properties={
                    "trigger_source": "watchdog",
                    "failure_kind": "verification_failed",
                },
            )
            await record_first_run_outcome(
                run, AutomationRunStatus.FAILED, "watchdog", session=session
            )
        return result.rowcount > 0

    if verification.outcome in (
        VerificationOutcome.COMPLETED,
        VerificationOutcome.FAILED,
    ):
        exit_code = verification.exit_code

        # Command completed successfully; the callback was missed.
        if verification.outcome == VerificationOutcome.COMPLETED:
            logger.info(
                "Verified run completed successfully (exit_code=%s), "
                "callback was missed",
                exit_code,
                extra=extra,
            )
            stmt = (
                update(AutomationRun)
                .where(
                    AutomationRun.id == run.id,
                    AutomationRun.status == AutomationRunStatus.RUNNING,
                )
                .values(
                    status=AutomationRunStatus.COMPLETED,
                    completed_at=now,
                    status_detail=None,
                )
            )

        # exit_code == -1 or None: Command was killed/timed out by bash service
        elif exit_code is None or exit_code == -1:
            error_msg = "command timed out or was killed"
            if verification.stderr:
                error_msg += f"\nstderr: {verification.stderr[-1000:]}"

            logger.warning(
                "Run timed out (exit_code=%s)",
                exit_code,
                extra=extra,
            )
            error_detail = f"Timed out: {error_msg}"
            stmt = (
                update(AutomationRun)
                .where(
                    AutomationRun.id == run.id,
                    AutomationRun.status == AutomationRunStatus.RUNNING,
                )
                .values(
                    status=AutomationRunStatus.FAILED,
                    completed_at=now,
                    error_detail=error_detail,
                    status_detail=make_run_status_detail(
                        phase=RunStatusPhase.EXECUTION,
                        kind=RunStatusDetailKind.TIMEOUT,
                        detail=error_msg,
                        transient=False,
                        source="agent_server",
                        operation="verify_run",
                        code=str(exit_code) if exit_code is not None else None,
                        previous=run.status_detail,
                        extra={"formatted_detail": error_detail},
                    ),
                )
            )

        # Any other exit code: Command failed with an actual error
        else:
            error_parts = [f"exit_code={exit_code}"]
            if verification.stderr:
                error_parts.append(f"stderr: {verification.stderr[-1000:]}")
            if verification.stdout:
                error_parts.append(f"stdout: {verification.stdout[-500:]}")
            error_detail = "\n".join(error_parts)

            logger.warning(
                "Verified run failed (exit_code=%s)",
                exit_code,
                extra=extra,
            )
            stmt = (
                update(AutomationRun)
                .where(
                    AutomationRun.id == run.id,
                    AutomationRun.status == AutomationRunStatus.RUNNING,
                )
                .values(
                    status=AutomationRunStatus.FAILED,
                    completed_at=now,
                    error_detail=error_detail,
                    status_detail=make_run_status_detail(
                        phase=RunStatusPhase.EXECUTION,
                        kind=RunStatusDetailKind.EXECUTION_ERROR,
                        detail=error_detail,
                        transient=False,
                        source="agent_server",
                        operation="verify_run",
                        code=str(exit_code),
                        previous=run.status_detail,
                    ),
                )
            )

        result = await session.execute(stmt)  # type: ignore[assignment]
        if result.rowcount > 0:
            await capture_automation_event(
                "automation_run_completed"
                if exit_code == 0
                else "automation_run_failed",
                automation=_loaded_automation(run),
                run=run,
                session=session,
                properties={
                    "trigger_source": "watchdog",
                    "verification_exit_code": exit_code,
                },
            )
            await record_first_run_outcome(
                run,
                AutomationRunStatus.COMPLETED
                if exit_code == 0
                else AutomationRunStatus.FAILED,
                "watchdog",
                session=session,
            )
        if result.rowcount > 0:
            keep_alive = await _get_automation_keep_alive(session, run)
            if _should_cleanup_sandbox_after_terminal(run, keep_alive):
                try:
                    await backend.cleanup_after_verification(run_id)
                except Exception as e:
                    logger.warning(
                        "Cleanup after terminal verification failed: %s",
                        e,
                        extra=extra,
                    )
        return result.rowcount > 0

    # Verification failed - execution environment not available or command still running
    if verification.outcome == VerificationOutcome.TRANSIENT_ERROR:
        logger.warning(
            "Verification temporarily unavailable; leaving run RUNNING: %s",
            verification.detail,
            extra=extra,
        )
        if verification.error_info is not None:
            status_detail = run_status_detail_from_transient_error(
                verification.error_info,
                phase=RunStatusPhase.VERIFICATION,
                previous=run.status_detail,
            )
        else:
            status_detail = make_run_status_detail(
                phase=RunStatusPhase.VERIFICATION,
                kind=RunStatusDetailKind.UNKNOWN,
                detail=verification.detail or "Verification temporarily unavailable",
                transient=True,
                source="automation_service",
                operation="verify_run",
                previous=run.status_detail,
            )
        await session.execute(
            update(AutomationRun)
            .where(
                AutomationRun.id == run.id,
                AutomationRun.status == AutomationRunStatus.RUNNING,
            )
            .values(status_detail=status_detail)
        )
        return False

    # A still-running bash command is not a failure: its own timeout
    # (enforced by the agent-server) has not fired yet, so defer instead of
    # destroying a live run. Must happen before any cleanup below.
    if verification.outcome == VerificationOutcome.STILL_RUNNING:
        if await _defer_still_running(session, run, settings, now):
            return False
        logger.warning(
            "Still-running grace exhausted, proceeding to terminal timeout",
            extra=extra,
        )

    # This likely means the sandbox crashed, was cleaned up, or verification
    # failed in a way that is not known to be transient.
    logger.warning(
        "Could not verify run status: %s, marking as timed out",
        verification.detail,
        extra=extra,
    )

    # Clean up resources via backend only when the automation owns explicit
    # cleanup. Otherwise, leave cleanup to the runtime TTL reaper.
    keep_alive = await _get_automation_keep_alive(session, run)
    if _should_cleanup_sandbox_after_terminal(run, keep_alive):
        try:
            await backend.cleanup_after_verification(run_id)
        except Exception as e:
            logger.warning("Cleanup after verification failed: %s", e, extra=extra)

    error_msg = verification.detail or "no completion callback received"

    logger.warning(
        "Marking run as timed out: run_id=%s, sandbox_id=%s, timeout_at=%s, reason=%s",
        run_id,
        sandbox_id,
        run.timeout_at,
        error_msg,
        extra=extra,
    )

    stmt = (
        update(AutomationRun)
        .where(
            AutomationRun.id == run.id,
            AutomationRun.status == AutomationRunStatus.RUNNING,
        )
        .values(
            status=AutomationRunStatus.FAILED,
            completed_at=now,
            error_detail=f"Timed out: {error_msg}",
            status_detail=make_run_status_detail(
                phase=RunStatusPhase.VERIFICATION,
                kind=(
                    RunStatusDetailKind.ENVIRONMENT_UNAVAILABLE
                    if verification.outcome
                    == VerificationOutcome.ENVIRONMENT_UNAVAILABLE
                    else RunStatusDetailKind.TIMEOUT
                ),
                detail=error_msg,
                transient=False,
                source="automation_service",
                operation="verify_run",
                previous=run.status_detail,
                extra={
                    "verification_outcome": (
                        verification.outcome.value
                        if verification.outcome
                        else "unknown"
                    )
                },
            ),
        )
    )
    result = await session.execute(stmt)  # type: ignore[assignment]
    if result.rowcount > 0:
        await capture_automation_event(
            "automation_run_failed",
            automation=_loaded_automation(run),
            run=run,
            session=session,
            properties={
                "trigger_source": "watchdog",
                "failure_kind": "timeout",
            },
        )
        await record_first_run_outcome(
            run, AutomationRunStatus.FAILED, "watchdog", session=session
        )
    return result.rowcount > 0


async def mark_stale_runs(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> int:
    """Find and process stale RUNNING runs.

    A run is stale if ``timeout_at < now()``. Before marking as FAILED,
    attempts to verify the actual status by querying the sandbox. Uses
    optimistic locking so concurrent callbacks win.

    Each run is processed in its own session so that row locks are released
    immediately after commit rather than held for the duration of the batch.
    This prevents lock contention with concurrent callback UPDATEs.

    Returns the number of runs marked with terminal status.
    """
    now = utcnow()
    marked = 0

    async with session_factory() as session:
        # Fetch stale run IDs only — close this session before doing any
        # per-run work so we don't hold locks across slow verify calls.
        result = await session.execute(
            select(AutomationRun.id).where(
                AutomationRun.status == AutomationRunStatus.RUNNING,
                AutomationRun.timeout_at.isnot(None),
                AutomationRun.timeout_at < now,
            )
        )
        stale_run_ids = list(result.scalars().all())

    for run_id in stale_run_ids:
        async with session_factory() as session:
            # Re-fetch with automation relationship inside a fresh session.
            result = await session.execute(
                select(AutomationRun)
                .options(selectinload(AutomationRun.automation))
                .where(AutomationRun.id == run_id)
            )
            run = result.scalars().first()
            if run is None:
                continue

            extra = log_extra(run_id=str(run_id), sandbox_id=run.sandbox_id)

            logger.info(
                "Processing stale run (timeout_at=%s, now=%s)",
                run.timeout_at,
                now,
                extra=extra,
            )

            try:
                # Commit unconditionally: a non-terminal outcome may still
                # have deferred timeout_at, and that UPDATE must persist.
                terminal = await _verify_and_mark_run(session, run, settings)
                await session.commit()
                if terminal:
                    marked += 1
                else:
                    logger.info(
                        "Run not terminal (completed concurrently or deferred)",
                        extra=extra,
                    )
            except Exception:
                logger.exception("Error processing stale run", extra=extra)

    return marked


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
    return local_runs_root(workspace_base).resolve(strict=False)


def _workspace_path(
    workspace_base: str | os.PathLike[str] | None, run_id: UUID
) -> Path:
    """Build a run path from a typed UUID, never from raw path input."""
    return _workspace_root(workspace_base) / str(run_id)


def _scan_candidates(runs_root: Path) -> dict[UUID, float]:
    """Find direct, canonical UUID workspace directories under runs_root."""
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
            # followlinks=False; prune them so external targets are not counted.
            dirnames[:] = [
                name
                for name in dirnames
                if not _is_link_or_junction(Path(dirpath) / name)
            ]
            for filename in filenames:
                # Read the link itself rather than its target.
                with suppress(OSError):
                    total += (
                        (Path(dirpath) / filename).stat(follow_symlinks=False).st_size
                    )
    return total


def _is_link_or_junction(path: Path) -> bool:
    """Return whether path is a symlink or Windows directory junction."""
    return path.is_symlink() or path.is_junction()


def _is_expired(timestamp: datetime | float, cutoff: datetime) -> bool:
    """Return whether a trusted timestamp is older than cutoff."""
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
    batch_size: int = WORKSPACE_PURGE_BATCH_SIZE,
    shutdown_event: asyncio.Event | None = None,
    deferred_last_cycle: set[UUID] | None = None,
) -> PurgeResult:
    """Purge expired terminal and inactive orphan workspace directories.

    The module batch constant bounds both the successful deletions and the
    classification candidate set. Failed/refused attempts and candidates that
    are not actionable this cycle are remembered and moved behind fresh
    candidates on the next cycle.
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

    max_attempts = batch_size * DELETE_ATTEMPT_FACTOR
    previous_deferred = (
        deferred_last_cycle if deferred_last_cycle is not None else set()
    )
    ordered_candidates = sorted(
        candidates.items(),
        key=lambda item: (
            item[0] in previous_deferred,
            item[1],
            str(item[0]),
        ),
    )[:max_attempts]

    by_id: dict[UUID, tuple[AutomationRunStatus, datetime | None]] = {}
    candidate_ids = [run_id for run_id, _root_mtime in ordered_candidates]
    if candidate_ids:
        # The single query is safe because the candidate list is bounded by
        # the deletion-attempt budget (150 entries with the default settings).
        async with session_factory() as session:
            stmt = select(
                AutomationRun.id,
                AutomationRun.status,
                AutomationRun.completed_at,
            ).where(AutomationRun.id.in_(candidate_ids))
            rows = (await session.execute(stmt)).all()
            by_id.update(
                (run_id, (status, completed_at))
                for run_id, status, completed_at in rows
            )

    deleted = missing = refused = errors = bytes_freed = attempts = 0
    deferred_this_cycle: set[UUID] = set()
    for run_id, root_mtime in ordered_candidates:
        if deleted >= batch_size or attempts >= max_attempts:
            break
        if shutdown_event is not None and shutdown_event.is_set():
            break

        row = by_id.get(run_id)
        if row is not None:
            status, completed_at = row
            if not _is_terminal(status):
                deferred_this_cycle.add(run_id)
                continue
        else:
            completed_at = None

        # Service-owned live runs retain an AutomationRun row. Orphans and
        # terminal rows without completed_at use the scandir-captured root mtime
        # as their inactivity guard.
        age = completed_at if completed_at is not None else root_mtime
        if not _is_expired(age, cutoff):
            deferred_this_cycle.add(run_id)
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
                deferred_this_cycle.add(run_id)
            case DeleteOutcome.ERROR:
                errors += 1
                deferred_this_cycle.add(run_id)
            case DeleteOutcome.DELETED:
                deleted += 1
                bytes_freed += delete_result.bytes_freed
                logger.info(
                    "Purged workspace for run %s (%d bytes freed)",
                    run_id,
                    delete_result.bytes_freed,
                )

    if deferred_last_cycle is not None:
        deferred_last_cycle.clear()
        deferred_last_cycle.update(deferred_this_cycle)

    result = PurgeResult(
        len(candidates), deleted, missing, refused, errors, bytes_freed
    )
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


async def prune_integration_events(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> int:
    """Delete accepted events past the retention window, one batch per scan."""
    cutoff = utcnow() - timedelta(days=settings.integration_event_retention_days)

    async with session_factory() as session:
        result: CursorResult = await session.execute(  # type: ignore[assignment]
            delete(IntegrationEvent).where(
                IntegrationEvent.id.in_(
                    select(IntegrationEvent.id)
                    .where(IntegrationEvent.received_at < cutoff)
                    .limit(PRUNE_BATCH_SIZE)
                )
            )
        )
        await session.commit()

    return result.rowcount


async def watchdog_loop(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    shutdown_event: asyncio.Event | None = None,
) -> None:
    """Main watchdog loop — scans for stale runs periodically.

    Args:
        session_factory: Async session maker for database access.
        settings: Application settings.
        shutdown_event: Event to signal graceful shutdown.
    """
    interval = settings.watchdog_interval_seconds

    logger.info(
        "Watchdog started, scanning every %ds",
        interval,
    )
    deferred_workspace_ids: set[UUID] = set()

    while True:
        if shutdown_event is not None and shutdown_event.is_set():
            logger.info("Watchdog received shutdown signal, exiting")
            break

        try:
            marked = await mark_stale_runs(session_factory, settings)
            if marked:
                logger.info("Processed %d stale run(s)", marked)
        except Exception:
            logger.exception("Error in watchdog scan")

        try:
            pruned = await prune_integration_events(session_factory, settings)
            if pruned:
                logger.info("Pruned %d expired integration event(s)", pruned)
        except Exception:
            logger.exception("Error pruning integration events")

        if settings.is_local_mode and settings.workspace_retention_seconds > 0:
            try:
                await purge_terminal_workspaces(
                    session_factory=session_factory,
                    workspace_base=settings.workspace_base,
                    retention_seconds=settings.workspace_retention_seconds,
                    deferred_last_cycle=deferred_workspace_ids,
                )
            except Exception:
                logger.exception("Error purging workspace directories")

        if shutdown_event is not None:
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
                logger.info("Watchdog received shutdown signal, exiting")
                break
            except TimeoutError:
                pass
        else:
            await asyncio.sleep(interval)
