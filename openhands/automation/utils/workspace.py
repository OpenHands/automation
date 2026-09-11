"""Guarded deletion of local-mode automation run workspaces.

Shared by the watchdog's retention purge and the local backend's cleanup on
terminal state, so a recursive delete is guarded in exactly one place.
"""

from __future__ import annotations

import logging
import os
import shutil
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from uuid import UUID


logger = logging.getLogger("automation.workspace")


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


def is_link_or_junction(path: Path) -> bool:
    """Return whether path is a symlink or Windows directory junction."""
    return path.is_symlink() or path.is_junction()


def dir_size(path: Path) -> int:
    """Best-effort reclaimed-space estimate; may undercount on races."""
    total = 0
    with suppress(OSError):
        for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
            # Unlike symlinks, os.walk descends Windows junctions even with
            # followlinks=False; prune them so external targets are not counted.
            dirnames[:] = [
                name
                for name in dirnames
                if not is_link_or_junction(Path(dirpath) / name)
            ]
            for filename in filenames:
                # Read the link itself rather than its target.
                with suppress(OSError):
                    total += (
                        (Path(dirpath) / filename).stat(follow_symlinks=False).st_size
                    )
    return total


def workspace_path(runs_root: Path, run_id: UUID) -> Path:
    """Build a run path from a typed UUID, never from raw path input."""
    return runs_root / str(run_id)


def delete_workspace(runs_root: Path, run_id: UUID) -> WorkspaceDeleteResult:
    """Delete one verified run directory without following root reparse points."""
    path = workspace_path(runs_root, run_id)

    if is_link_or_junction(runs_root):
        logger.warning("Refusing linked workspace root: %s", runs_root)
        return WorkspaceDeleteResult(DeleteOutcome.REFUSED, 0)
    if is_link_or_junction(path):
        logger.warning("Refusing linked workspace path: %s", path)
        return WorkspaceDeleteResult(DeleteOutcome.REFUSED, 0)
    if not path.exists():
        return WorkspaceDeleteResult(DeleteOutcome.MISSING, 0)

    try:
        resolved_root = runs_root.resolve(strict=True)
        resolved_path = path.resolve(strict=True)
    except FileNotFoundError:
        return WorkspaceDeleteResult(DeleteOutcome.MISSING, 0)
    except OSError as exc:
        logger.warning("Failed to resolve workspace %s: %s", path, exc)
        return WorkspaceDeleteResult(DeleteOutcome.ERROR, 0)

    if resolved_path.parent != resolved_root or not resolved_path.is_dir():
        logger.warning("Refusing workspace outside expected root: %s", path)
        return WorkspaceDeleteResult(DeleteOutcome.REFUSED, 0)

    size = dir_size(path)
    try:
        shutil.rmtree(path)
        return WorkspaceDeleteResult(DeleteOutcome.DELETED, size)
    except FileNotFoundError:
        return WorkspaceDeleteResult(DeleteOutcome.MISSING, 0)
    except OSError as exc:
        logger.warning("Failed to delete workspace %s: %s", path, exc)
        return WorkspaceDeleteResult(DeleteOutcome.ERROR, 0)
