"""Tests for workspace purging of local-mode terminal runs."""

import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import openhands.automation.workspace_cleaner as cleaner
from openhands.automation.models import (
    Automation,
    AutomationRun,
    AutomationRunStatus,
    Base,
)
from openhands.automation.utils import utcnow
from openhands.automation.workspace_cleaner import (
    DeleteOutcome,
    PurgeResult,
    _delete_workspace,
    _dir_size,
    _workspace_path,
    purge_terminal_workspaces,
    purger_loop,
)


@pytest.fixture
async def db_session_factory() -> AsyncGenerator[
    async_sessionmaker[AsyncSession], None
]:
    """Provide a network-free SQLite database for cleanup tests."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
def workspace_base(tmp_path) -> str:
    """Keep every filesystem deletion inside the current test temp directory."""
    return str(tmp_path / "workspace")


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


def _make_workspace(base: str, run_id: uuid.UUID, content: str = "data") -> Path:
    path = _workspace_path(base, run_id)
    path.mkdir(parents=True, exist_ok=True)
    (path / "output.txt").write_text(content, encoding="utf-8")
    return path


class TestWorkspacePath:
    def test_joins_base_and_run_id(self):
        run_id = _run_id()
        path = _workspace_path("/ws", run_id)
        assert path == Path("/ws/automation-runs").resolve() / str(run_id)

    def test_native_and_forward_slash_bases_normalize_identically(self, tmp_path):
        run_id = _run_id()
        native_path = _workspace_path(str(tmp_path), run_id)
        forward_slash_path = _workspace_path(tmp_path.as_posix(), run_id)
        assert native_path == forward_slash_path


class TestDirSize:
    def test_empty_dir(self, workspace_base):
        path = _workspace_path(workspace_base, _run_id())
        path.mkdir(parents=True)
        assert _dir_size(path) == 0

    def test_non_empty_dir(self, workspace_base):
        path = _make_workspace(workspace_base, _run_id(), "hello world")
        size = _dir_size(path)
        assert size == len("hello world")

    def test_missing_dir(self, workspace_base):
        path = _workspace_path(workspace_base, _run_id())
        assert _dir_size(path) == 0


class TestDeleteWorkspace:
    def test_deletes_existing(self, workspace_base):
        run_id = _run_id()
        path = _make_workspace(workspace_base, run_id)
        delete_result = _delete_workspace(workspace_base, run_id)
        assert delete_result.outcome is DeleteOutcome.DELETED
        assert delete_result.bytes_freed > 0
        assert not path.exists()

    def test_missing_is_idempotent(self, workspace_base):
        delete_result = _delete_workspace(workspace_base, _run_id())
        assert delete_result.outcome is DeleteOutcome.MISSING
        assert delete_result.bytes_freed == 0

    def test_empty_dir(self, workspace_base):
        run_id = _run_id()
        path = _workspace_path(workspace_base, run_id)
        path.mkdir(parents=True)
        delete_result = _delete_workspace(workspace_base, run_id)
        assert delete_result.outcome is DeleteOutcome.DELETED
        assert delete_result.bytes_freed == 0
        assert not path.exists()

    def test_refuses_file_instead_of_directory(self, workspace_base):
        run_id = _run_id()
        path = _workspace_path(workspace_base, run_id)
        path.parent.mkdir(parents=True)
        path.write_text("not a workspace directory", encoding="utf-8")

        delete_result = _delete_workspace(workspace_base, run_id)

        assert delete_result.outcome is DeleteOutcome.REFUSED
        assert path.exists()

    def test_refuses_symlink_escape(self, workspace_base, tmp_path):
        run_id = _run_id()
        outside = tmp_path / "outside"
        outside.mkdir()
        marker = outside / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        link = _workspace_path(workspace_base, run_id)
        link.parent.mkdir(parents=True)
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"directory symlinks unavailable: {exc}")

        delete_result = _delete_workspace(workspace_base, run_id)

        assert delete_result.outcome is DeleteOutcome.REFUSED
        assert marker.exists()

    def test_refuses_detected_junction(self, workspace_base, monkeypatch):
        run_id = _run_id()
        path = _make_workspace(workspace_base, run_id)
        monkeypatch.setattr(
            cleaner,
            "_is_link_or_junction",
            lambda candidate: candidate == path,
        )

        delete_result = _delete_workspace(workspace_base, run_id)

        assert delete_result.outcome is DeleteOutcome.REFUSED
        assert path.exists()


class TestPurgeTerminalWorkspaces:
    async def test_never_purges_active_runs(self, db_session_factory, workspace_base):
        """PENDING and RUNNING runs must never be purged."""
        auto_id = uuid.uuid4()
        await _create_automation(db_session_factory, auto_id)

        pending_id = _run_id()
        running_id = _run_id()
        old_time = utcnow() - timedelta(days=30)
        await _create_run(
            db_session_factory,
            pending_id,
            auto_id,
            AutomationRunStatus.PENDING,
            completed_at=old_time,
        )
        await _create_run(
            db_session_factory,
            running_id,
            auto_id,
            AutomationRunStatus.RUNNING,
            completed_at=old_time,
        )
        pending_path = _make_workspace(workspace_base, pending_id)
        running_path = _make_workspace(workspace_base, running_id)

        result = await purge_terminal_workspaces(
            db_session_factory, workspace_base, retention_seconds=0, batch_size=10
        )

        assert result.candidates_found == 0
        assert result.deleted == 0
        assert pending_path.exists()
        assert running_path.exists()

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
            completed_at=utcnow() - timedelta(seconds=60),
        )
        path = _make_workspace(workspace_base, run_id)

        result = await purge_terminal_workspaces(
            db_session_factory, workspace_base, retention_seconds=3600, batch_size=10
        )

        assert result.candidates_found == 0
        assert result.deleted == 0
        assert path.exists()

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
            completed_at=utcnow() - timedelta(days=30),
        )
        path = _make_workspace(workspace_base, run_id, "some data here")
        assert path.is_dir()

        result = await purge_terminal_workspaces(
            db_session_factory, workspace_base, retention_seconds=3600, batch_size=10
        )

        assert result.candidates_found == 1
        assert result.deleted == 1
        assert result.errors == 0
        assert result.bytes_freed == len("some data here")
        assert not path.exists()

    async def test_all_terminal_states(self, db_session_factory, workspace_base):
        """All terminal states are purged."""
        auto_id = uuid.uuid4()
        await _create_automation(db_session_factory, auto_id)

        old_time = utcnow() - timedelta(days=30)
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
            _make_workspace(workspace_base, run_id)

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
            completed_at=utcnow() - timedelta(days=30),
        )

        result = await purge_terminal_workspaces(
            db_session_factory, workspace_base, retention_seconds=3600, batch_size=10
        )

        assert result.candidates_found == 1
        assert result.deleted == 0
        assert result.missing == 1
        assert result.errors == 0

    async def test_respects_batch_size(self, db_session_factory, workspace_base):
        """Only batch_size workspaces are purged per call."""
        auto_id = uuid.uuid4()
        await _create_automation(db_session_factory, auto_id)

        old_time = utcnow() - timedelta(days=30)
        for _ in range(5):
            run_id = _run_id()
            await _create_run(
                db_session_factory,
                run_id,
                auto_id,
                AutomationRunStatus.COMPLETED,
                completed_at=old_time,
            )
            _make_workspace(workspace_base, run_id)

        result = await purge_terminal_workspaces(
            db_session_factory, workspace_base, retention_seconds=3600, batch_size=3
        )

        assert result.candidates_found == 3
        assert result.deleted == 3
        remaining = list((Path(workspace_base) / "automation-runs").iterdir())
        assert len(remaining) == 2

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
        path = _make_workspace(workspace_base, run_id)

        result = await purge_terminal_workspaces(
            db_session_factory, workspace_base, retention_seconds=0, batch_size=10
        )

        assert result.candidates_found == 0
        assert result.deleted == 0
        assert path.exists()

    async def test_workspace_base_expansion(
        self, db_session_factory, tmp_path, monkeypatch
    ):
        """`~` in workspace_base is expanded."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        auto_id = uuid.uuid4()
        await _create_automation(db_session_factory, auto_id)

        old_time = utcnow() - timedelta(days=30)
        run_id = _run_id()
        await _create_run(
            db_session_factory,
            run_id,
            auto_id,
            AutomationRunStatus.COMPLETED,
            completed_at=old_time,
        )
        path = _make_workspace("~", run_id)

        result = await purge_terminal_workspaces(
            db_session_factory,
            "~",
            retention_seconds=3600,
            batch_size=10,
        )

        assert result.candidates_found == 1
        assert result.deleted == 1
        assert result.errors == 0
        assert not path.exists()

    async def test_missing_old_row_does_not_starve_existing_workspace(
        self, db_session_factory, workspace_base
    ):
        auto_id = uuid.uuid4()
        await _create_automation(db_session_factory, auto_id)
        old_time = utcnow() - timedelta(days=30)

        missing_id = uuid.UUID(int=1)
        existing_id = uuid.UUID(int=2)
        await _create_run(
            db_session_factory,
            missing_id,
            auto_id,
            AutomationRunStatus.COMPLETED,
            completed_at=old_time - timedelta(seconds=1),
        )
        await _create_run(
            db_session_factory,
            existing_id,
            auto_id,
            AutomationRunStatus.COMPLETED,
            completed_at=old_time,
        )
        existing_path = _make_workspace(workspace_base, existing_id)

        result = await purge_terminal_workspaces(
            db_session_factory, workspace_base, retention_seconds=3600, batch_size=1
        )

        assert result.candidates_found == 2
        assert result.missing == 1
        assert result.deleted == 1
        assert not existing_path.exists()

    async def test_partially_created_workspace_is_deleted(
        self, db_session_factory, workspace_base
    ):
        auto_id = uuid.uuid4()
        await _create_automation(db_session_factory, auto_id)
        run_id = _run_id()
        await _create_run(
            db_session_factory,
            run_id,
            auto_id,
            AutomationRunStatus.FAILED,
            completed_at=utcnow() - timedelta(days=30),
        )
        path = _workspace_path(workspace_base, run_id)
        path.mkdir(parents=True)

        result = await purge_terminal_workspaces(
            db_session_factory, workspace_base, retention_seconds=3600, batch_size=1
        )

        assert result.deleted == 1
        assert result.bytes_freed == 0
        assert not path.exists()

    async def test_delete_error_is_retryable(
        self, db_session_factory, workspace_base, monkeypatch
    ):
        auto_id = uuid.uuid4()
        await _create_automation(db_session_factory, auto_id)
        run_id = _run_id()
        await _create_run(
            db_session_factory,
            run_id,
            auto_id,
            AutomationRunStatus.COMPLETED,
            completed_at=utcnow() - timedelta(days=30),
        )
        path = _make_workspace(workspace_base, run_id)
        real_rmtree = cleaner.shutil.rmtree

        def fail_rmtree(_path):
            raise PermissionError("locked")

        monkeypatch.setattr(cleaner.shutil, "rmtree", fail_rmtree)

        first_result = await purge_terminal_workspaces(
            db_session_factory, workspace_base, retention_seconds=3600, batch_size=1
        )

        assert first_result.errors == 1
        assert first_result.deleted == 0
        assert path.exists()

        monkeypatch.setattr(cleaner.shutil, "rmtree", real_rmtree)
        retry_result = await purge_terminal_workspaces(
            db_session_factory, workspace_base, retention_seconds=3600, batch_size=1
        )

        assert retry_result.deleted == 1
        assert retry_result.errors == 0
        assert not path.exists()

    @pytest.mark.parametrize(
        ("retention_seconds", "batch_size", "message"),
        [(-1, 1, "retention_seconds"), (0, 0, "batch_size")],
    )
    async def test_rejects_invalid_limits(
        self,
        db_session_factory,
        workspace_base,
        retention_seconds,
        batch_size,
        message,
    ):
        with pytest.raises(ValueError, match=message):
            await purge_terminal_workspaces(
                db_session_factory,
                workspace_base,
                retention_seconds=retention_seconds,
                batch_size=batch_size,
            )


class TestPurgerLoop:
    async def test_runs_immediately_then_stops_on_shutdown(self, monkeypatch):
        shutdown_event = cleaner.asyncio.Event()

        async def purge_once(**_kwargs):
            shutdown_event.set()
            return PurgeResult()

        mock_purge = AsyncMock(side_effect=purge_once)
        monkeypatch.setattr(cleaner, "purge_terminal_workspaces", mock_purge)

        await purger_loop(
            session_factory=AsyncMock(),
            workspace_base="/workspace",
            retention_seconds=60,
            interval_seconds=3600,
            batch_size=10,
            shutdown_event=shutdown_event,
        )

        mock_purge.assert_awaited_once()

    async def test_rejects_non_positive_interval(self):
        with pytest.raises(ValueError, match="interval_seconds"):
            await purger_loop(
                session_factory=AsyncMock(),
                workspace_base="/workspace",
                retention_seconds=60,
                interval_seconds=0,
            )


class TestPurgeResult:
    def test_default_values(self):
        result = PurgeResult()
        assert result.candidates_found == 0
        assert result.deleted == 0
        assert result.missing == 0
        assert result.refused == 0
        assert result.errors == 0
        assert result.bytes_freed == 0

    def test_partial_success(self):
        result = PurgeResult(
            candidates_found=10,
            deleted=8,
            missing=1,
            refused=1,
            errors=2,
            bytes_freed=1024,
        )
        assert result.candidates_found == 10
        assert result.deleted == 8
        assert result.missing == 1
        assert result.refused == 1
        assert result.errors == 2
        assert result.bytes_freed == 1024
