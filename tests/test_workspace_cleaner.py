"""Tests for workspace purging of local-mode terminal runs."""

import os
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from openhands.automation.models import Automation, AutomationRun, AutomationRunStatus
from openhands.automation.workspace_cleaner import (
    PurgeResult,
    _delete_workspace,
    _dir_size,
    _workspace_path,
    purge_terminal_workspaces,
)


async def _create_automation(
    session_factory: async_sessionmaker[AsyncSession],
    auto_id: uuid.UUID,
) -> None:
    async with session_factory() as session:
        auto = Automation(
            id=auto_id,
            user_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            name="test-auto",
            trigger={"type": "manual"},
            tarball_path="s3://test/tarball.tar.gz",
            entrypoint="echo hello",
        )
        session.add(auto)
        await session.commit()


def _run_id() -> uuid.UUID:
    return uuid.uuid4()


async def _create_run(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    automation_id: uuid.UUID,
    status: AutomationRunStatus,
    completed_at: datetime | None = None,
) -> None:
    async with session_factory() as session:
        run = AutomationRun(
            id=run_id,
            automation_id=automation_id,
            status=status,
            completed_at=completed_at,
        )
        session.add(run)
        await session.commit()


def _make_workspace(base: str, run_id: str, content: str = "data") -> str:
    path = _workspace_path(base, run_id)
    os.makedirs(path, exist_ok=True)
    file_path = os.path.join(path, "output.txt")
    with open(file_path, "w") as f:
        f.write(content)
    return path


class TestWorkspacePath:
    def test_joins_base_and_run_id(self):
        path = _workspace_path("/ws", "abc-123")
        assert path == os.path.join("/ws", "automation-runs", "abc-123")


class TestDirSize:
    def test_empty_dir(self, workspace_base):
        path = _workspace_path(workspace_base, "empty-run")
        os.makedirs(path, exist_ok=True)
        assert _dir_size(path) == 0

    def test_non_empty_dir(self, workspace_base):
        path = _make_workspace(workspace_base, "data-run", "hello world")
        size = _dir_size(path)
        assert size == len("hello world")

    def test_missing_dir(self, workspace_base):
        path = _workspace_path(workspace_base, "missing-run")
        assert _dir_size(path) == 0


class TestDeleteWorkspace:
    def test_deletes_existing(self, workspace_base):
        path = _make_workspace(workspace_base, "del-run")
        assert os.path.isdir(path)
        bytes_freed = _delete_workspace(path)
        assert bytes_freed is not None
        assert bytes_freed > 0
        assert not os.path.exists(path)

    def test_missing_is_idempotent(self, workspace_base):
        path = _workspace_path(workspace_base, "already-gone")
        bytes_freed = _delete_workspace(path)
        assert bytes_freed == 0

    def test_empty_dir(self, workspace_base):
        path = _workspace_path(workspace_base, "empty")
        os.makedirs(path, exist_ok=True)
        bytes_freed = _delete_workspace(path)
        assert bytes_freed == 0
        assert not os.path.exists(path)


class TestPurgeTerminalWorkspaces:
    async def test_never_purges_active_runs(self, db_session_factory, workspace_base):
        """PENDING and RUNNING runs must never be purged."""
        auto_id = uuid.uuid4()
        await _create_automation(db_session_factory, auto_id)

        pending_id = _run_id()
        running_id = _run_id()
        await _create_run(
            db_session_factory, pending_id, auto_id, AutomationRunStatus.PENDING
        )
        await _create_run(
            db_session_factory, running_id, auto_id, AutomationRunStatus.RUNNING
        )

        result = await purge_terminal_workspaces(
            db_session_factory, workspace_base, retention_seconds=0, batch_size=10
        )

        assert result.candidates_found == 0
        assert result.deleted == 0

    async def test_respects_retention_cutoff(self, db_session_factory, workspace_base):
        """Recent terminal runs must not be purged."""
        auto_id = uuid.uuid4()
        await _create_automation(db_session_factory, auto_id)

        run_id = _run_id()
        await _create_run(
            db_session_factory,
            run_id,
            auto_id,
            AutomationRunStatus.COMPLETED,
            completed_at=datetime.utcnow() - timedelta(seconds=60),
        )
        _make_workspace(workspace_base, str(run_id))

        result = await purge_terminal_workspaces(
            db_session_factory, workspace_base, retention_seconds=3600, batch_size=10
        )

        assert result.candidates_found == 0
        assert result.deleted == 0

    async def test_purges_old_terminal_workspace(
        self, db_session_factory, workspace_base
    ):
        """Old terminal run workspaces must be purged."""
        auto_id = uuid.uuid4()
        await _create_automation(db_session_factory, auto_id)

        run_id = _run_id()
        await _create_run(
            db_session_factory,
            run_id,
            auto_id,
            AutomationRunStatus.FAILED,
            completed_at=datetime.utcnow() - timedelta(days=30),
        )
        path = _make_workspace(workspace_base, str(run_id), "some data here")
        assert os.path.isdir(path)

        result = await purge_terminal_workspaces(
            db_session_factory, workspace_base, retention_seconds=3600, batch_size=10
        )

        assert result.candidates_found == 1
        assert result.deleted == 1
        assert result.errors == 0
        assert result.bytes_freed == len("some data here")
        assert not os.path.exists(path)

    async def test_all_terminal_states(self, db_session_factory, workspace_base):
        """All terminal states are purged."""
        auto_id = uuid.uuid4()
        await _create_automation(db_session_factory, auto_id)

        old_time = datetime.utcnow() - timedelta(days=30)
        for status in (
            AutomationRunStatus.COMPLETED,
            AutomationRunStatus.FAILED,
            AutomationRunStatus.CANCELLED,
            AutomationRunStatus.SKIPPED,
        ):
            run_id = _run_id()
            await _create_run(
                db_session_factory, run_id, auto_id, status, completed_at=old_time
            )
            _make_workspace(workspace_base, str(run_id))

        result = await purge_terminal_workspaces(
            db_session_factory, workspace_base, retention_seconds=3600, batch_size=10
        )

        assert result.candidates_found == 4
        assert result.deleted == 4
        assert result.errors == 0

    async def test_tolerates_missing_workspace(
        self, db_session_factory, workspace_base
    ):
        """Runs without workspace directories must not cause errors."""
        auto_id = uuid.uuid4()
        await _create_automation(db_session_factory, auto_id)

        run_id = _run_id()
        await _create_run(
            db_session_factory,
            run_id,
            auto_id,
            AutomationRunStatus.COMPLETED,
            completed_at=datetime.utcnow() - timedelta(days=30),
        )

        result = await purge_terminal_workspaces(
            db_session_factory, workspace_base, retention_seconds=3600, batch_size=10
        )

        assert result.candidates_found == 1
        assert result.deleted == 1
        assert result.errors == 0

    async def test_respects_batch_size(self, db_session_factory, workspace_base):
        """Only batch_size workspaces are purged per call."""
        auto_id = uuid.uuid4()
        await _create_automation(db_session_factory, auto_id)

        old_time = datetime.utcnow() - timedelta(days=30)
        for _ in range(5):
            run_id = _run_id()
            await _create_run(
                db_session_factory,
                run_id,
                auto_id,
                AutomationRunStatus.COMPLETED,
                completed_at=old_time,
            )
            _make_workspace(workspace_base, str(run_id))

        result = await purge_terminal_workspaces(
            db_session_factory, workspace_base, retention_seconds=3600, batch_size=3
        )

        assert result.candidates_found == 3
        assert result.deleted == 3

    async def test_empty_database(self, db_session_factory, workspace_base):
        """Purger handles an empty database gracefully."""
        result = await purge_terminal_workspaces(
            db_session_factory, workspace_base, retention_seconds=3600, batch_size=10
        )

        assert result.candidates_found == 0
        assert result.deleted == 0
        assert result.errors == 0

    async def test_skips_runs_without_completed_at(
        self, db_session_factory, workspace_base
    ):
        """Terminal runs without completed_at are skipped."""
        auto_id = uuid.uuid4()
        await _create_automation(db_session_factory, auto_id)

        run_id = _run_id()
        await _create_run(
            db_session_factory,
            run_id,
            auto_id,
            AutomationRunStatus.CANCELLED,
            completed_at=None,
        )
        _make_workspace(workspace_base, str(run_id))

        result = await purge_terminal_workspaces(
            db_session_factory, workspace_base, retention_seconds=0, batch_size=10
        )

        assert result.candidates_found == 0
        assert result.deleted == 0

    async def test_workspace_base_expansion(self, db_session_factory):
        """`~` in workspace_base is expanded."""
        auto_id = uuid.uuid4()
        await _create_automation(db_session_factory, auto_id)

        old_time = datetime.utcnow() - timedelta(days=30)
        run_id = _run_id()
        await _create_run(
            db_session_factory,
            run_id,
            auto_id,
            AutomationRunStatus.COMPLETED,
            completed_at=old_time,
        )

        result = await purge_terminal_workspaces(
            db_session_factory,
            os.path.expanduser("~"),
            retention_seconds=3600,
            batch_size=10,
        )

        assert result.candidates_found == 1
        assert result.deleted == 1
        assert result.errors == 0


class TestPurgeResult:
    def test_default_values(self):
        result = PurgeResult()
        assert result.candidates_found == 0
        assert result.deleted == 0
        assert result.errors == 0
        assert result.bytes_freed == 0

    def test_partial_success(self):
        result = PurgeResult(
            candidates_found=10,
            deleted=8,
            errors=2,
            bytes_freed=1024,
        )
        assert result.candidates_found == 10
        assert result.deleted == 8
        assert result.errors == 2
        assert result.bytes_freed == 1024
