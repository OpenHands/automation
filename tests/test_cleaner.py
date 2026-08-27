"""Tests for local-mode workspace cleaning of terminal runs."""

import asyncio
import logging
import os
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import openhands.automation.cleaner as cleaner
from openhands.automation.cleaner import (
    DB_CLASSIFICATION_CHUNK_SIZE,
    DELETE_ATTEMPT_FACTOR,
    DeleteOutcome,
    PurgeResult,
    _delete_workspace,
    _dir_size,
    _scan_candidates,
    _workspace_path,
    purge_terminal_workspaces,
    purger_loop,
)
from openhands.automation.models import (
    Automation,
    AutomationRun,
    AutomationRunStatus,
    Base,
)
from openhands.automation.utils import utcnow


def _create_junction(source: Path, destination: Path) -> None:
    """Create a real Windows directory junction; needs no special privileges."""
    import _winapi

    _winapi.CreateJunction(str(source), str(destination))


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


def _make_workspace(
    base: str,
    run_id: uuid.UUID,
    content: str = "data",
    mtime: datetime | None = None,
) -> Path:
    path = _workspace_path(base, run_id)
    path.mkdir(parents=True, exist_ok=True)
    (path / "output.txt").write_text(content, encoding="utf-8")
    if mtime is not None:
        timestamp = mtime.timestamp()
        os.utime(path / "output.txt", (timestamp, timestamp))
        os.utime(path, (timestamp, timestamp))
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


class TestCandidateScan:
    def test_skips_files_and_invalid_workspace_names(self, workspace_base):
        runs_root = Path(workspace_base) / "automation-runs"
        runs_root.mkdir(parents=True)
        valid_id = _run_id()
        (runs_root / str(valid_id)).mkdir()
        (runs_root / str(_run_id())).write_text("not a directory", encoding="utf-8")
        (runs_root / "not-a-uuid").mkdir()
        uppercase_id = _run_id()
        (runs_root / str(uppercase_id).upper()).mkdir()

        candidates = _scan_candidates(runs_root)

        assert list(candidates) == [valid_id]

    def test_skips_symlinked_workspace(self, workspace_base, tmp_path):
        run_id = _run_id()
        outside = tmp_path / "outside"
        outside.mkdir()
        marker = outside / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        runs_root = Path(workspace_base) / "automation-runs"
        runs_root.mkdir(parents=True)
        link = runs_root / str(run_id)
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"directory symlinks unavailable: {exc}")

        candidates = _scan_candidates(runs_root)

        assert candidates == {}
        assert marker.exists()

    def test_skips_real_junction_workspace(self, workspace_base, tmp_path):
        """A reparse-point directory is never scanned as a candidate."""
        real_workspace = tmp_path / "real-workspace"
        real_workspace.mkdir()
        (real_workspace / "data.txt").write_text("keep", encoding="utf-8")
        runs_root = Path(workspace_base) / "automation-runs"
        runs_root.mkdir(parents=True)
        try:
            _create_junction(real_workspace, runs_root / str(_run_id()))
        except (OSError, ImportError) as exc:
            pytest.skip(f"directory junctions unavailable: {exc}")

        candidates = _scan_candidates(runs_root)

        assert candidates == {}
        assert (real_workspace / "data.txt").exists()

    def test_bad_dirent_does_not_abort_other_candidates(
        self, workspace_base, monkeypatch
    ):
        runs_root = Path(workspace_base) / "automation-runs"
        run_ids = [_run_id() for _ in range(3)]
        for run_id in run_ids:
            (runs_root / str(run_id)).mkdir(parents=True, exist_ok=True)
        with os.scandir(runs_root) as entries:
            real_entries = list(entries)
        expected = {
            uuid.UUID(real_entries[0].name),
            uuid.UUID(real_entries[2].name),
        }

        class BadEntry:
            def __init__(self, entry):
                self._entry = entry

            def __getattr__(self, name):
                return getattr(self._entry, name)

            def is_symlink(self):
                raise OSError("stale entry")

        class Entries:
            def __enter__(self):
                return iter(
                    [real_entries[0], BadEntry(real_entries[1]), real_entries[2]]
                )

            def __exit__(self, *_args):
                return False

        monkeypatch.setattr(cleaner.os, "scandir", lambda _path: Entries())

        assert set(_scan_candidates(runs_root)) == expected

    def test_empty_runs_root_is_safe(self, workspace_base):
        runs_root = Path(workspace_base) / "automation-runs"
        runs_root.mkdir(parents=True)

        assert _scan_candidates(runs_root) == {}


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

    def test_symlink_target_bytes_are_not_counted(self, workspace_base, tmp_path):
        """Reclaimed-space accounting must not follow links out of the tree."""
        target = tmp_path / "outside"
        target.mkdir()
        (target / "blob.bin").write_text("z" * 4096, encoding="utf-8")
        path = _workspace_path(workspace_base, _run_id())
        path.mkdir(parents=True)
        (path / "small.txt").write_text("x", encoding="utf-8")
        try:
            (path / "linked.bin").symlink_to(target / "blob.bin")
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"file symlinks unavailable: {exc}")

        size = _dir_size(path)

        assert size < 4096
        assert (target / "blob.bin").exists()


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

    def test_refuses_symlinked_workspace_root(self, workspace_base, tmp_path):
        run_id = _run_id()
        outside = tmp_path / "outside-root"
        workspace = outside / str(run_id)
        workspace.mkdir(parents=True)
        marker = workspace / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        runs_root = cleaner._workspace_root(workspace_base)
        runs_root.parent.mkdir(parents=True)
        try:
            runs_root.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"directory symlinks unavailable: {exc}")

        delete_result = _delete_workspace(workspace_base, run_id)

        assert delete_result.outcome is DeleteOutcome.REFUSED
        assert marker.exists()

    def test_refuses_real_workspace_root_junction(self, workspace_base, tmp_path):
        """A junctioned runs root must never be recursively deleted."""
        run_id = _run_id()
        outside_runs = tmp_path / "outside-runs"
        workspace = outside_runs / str(run_id)
        workspace.mkdir(parents=True)
        marker = workspace / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        try:
            _create_junction(outside_runs, cleaner._workspace_root(workspace_base))
        except (OSError, ImportError) as exc:
            pytest.skip(f"directory junctions unavailable: {exc}")

        delete_result = _delete_workspace(workspace_base, run_id)

        assert delete_result.outcome is DeleteOutcome.REFUSED
        assert marker.exists()

    def test_refuses_real_junction_workspace_path(self, workspace_base, tmp_path):
        """A junctioned workspace candidate is refused instead of followed."""
        run_id = _run_id()
        real_workspace = tmp_path / "real-workspace"
        real_workspace.mkdir()
        marker = real_workspace / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        link = _workspace_path(workspace_base, run_id)
        try:
            _create_junction(real_workspace, link)
        except (OSError, ImportError) as exc:
            pytest.skip(f"directory junctions unavailable: {exc}")

        delete_result = _delete_workspace(workspace_base, run_id)

        assert delete_result.outcome is DeleteOutcome.REFUSED
        assert marker.exists()


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
            db_session_factory, workspace_base, retention_seconds=3600, batch_size=10
        )

        assert result.candidates_found == 2
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

        assert result.candidates_found == 1
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

    async def test_missing_database_workspace_is_not_a_filesystem_candidate(
        self, db_session_factory, workspace_base
    ):
        """Rows without directories are invisible to filesystem-first discovery."""
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

        assert result.candidates_found == 0
        assert result.deleted == 0
        assert result.missing == 0
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

        assert result.candidates_found == 5
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

    async def test_purges_old_orphan_workspace(
        self, db_session_factory, workspace_base
    ):
        """An old valid workspace without a database row is an orphan."""
        run_id = _run_id()
        path = _make_workspace(
            workspace_base,
            run_id,
            mtime=utcnow() - timedelta(days=30),
        )

        result = await purge_terminal_workspaces(
            db_session_factory, workspace_base, retention_seconds=3600, batch_size=10
        )

        assert result.candidates_found == 1
        assert result.deleted == 1
        assert not path.exists()

    async def test_keeps_young_orphan_workspace(
        self, db_session_factory, workspace_base
    ):
        """A young orphan remains until its filesystem retention expires."""
        run_id = _run_id()
        path = _make_workspace(workspace_base, run_id)

        result = await purge_terminal_workspaces(
            db_session_factory, workspace_base, retention_seconds=3600, batch_size=10
        )

        assert result.candidates_found == 1
        assert result.deleted == 0
        assert path.exists()

    async def test_classifies_orphan_and_database_rows_together(
        self, db_session_factory, workspace_base
    ):
        """Filesystem candidates are classified together by one DB snapshot."""
        auto_id = uuid.uuid4()
        await _create_automation(db_session_factory, auto_id)
        old_time = utcnow() - timedelta(days=30)

        orphan_id = _run_id()
        terminal_id = _run_id()
        pending_id = _run_id()
        await _create_run(
            db_session_factory,
            terminal_id,
            auto_id,
            AutomationRunStatus.COMPLETED,
            completed_at=old_time,
        )
        await _create_run(
            db_session_factory,
            pending_id,
            auto_id,
            AutomationRunStatus.PENDING,
            completed_at=old_time,
        )
        orphan_path = _make_workspace(workspace_base, orphan_id, mtime=old_time)
        terminal_path = _make_workspace(workspace_base, terminal_id)
        pending_path = _make_workspace(workspace_base, pending_id)

        result = await purge_terminal_workspaces(
            db_session_factory, workspace_base, retention_seconds=3600, batch_size=10
        )

        assert result.candidates_found == 3
        assert result.deleted == 2
        assert not orphan_path.exists()
        assert not terminal_path.exists()
        assert pending_path.exists()

    async def test_classification_queries_are_bounded_above_backend_ceiling(
        self, db_session_factory, workspace_base, monkeypatch
    ):
        candidate_count = 32_767
        candidates = {
            uuid.uuid4(): utcnow().timestamp() for _ in range(candidate_count)
        }
        monkeypatch.setattr(cleaner, "_scan_candidates", lambda _root: candidates)
        execute = AsyncSession.execute
        query_sizes = []

        async def spy_execute(session, statement, *args, **kwargs):
            values = next(iter(statement.compile().params.values()))
            query_sizes.append(len(values))
            return await execute(session, statement, *args, **kwargs)

        monkeypatch.setattr(AsyncSession, "execute", spy_execute)
        await purge_terminal_workspaces(
            db_session_factory, workspace_base, retention_seconds=3600, batch_size=10
        )

        assert len(query_sizes) > 1
        assert sum(query_sizes) == candidate_count
        assert max(query_sizes) <= DB_CLASSIFICATION_CHUNK_SIZE

    async def test_database_session_closes_before_deletion(
        self, db_session_factory, workspace_base, monkeypatch
    ):
        auto_id = uuid.uuid4()
        run_id = _run_id()
        await _create_automation(db_session_factory, auto_id)
        await _create_run(
            db_session_factory,
            run_id,
            auto_id,
            AutomationRunStatus.COMPLETED,
            completed_at=utcnow() - timedelta(days=30),
        )
        _make_workspace(workspace_base, run_id)
        session_closed = False

        class TrackedSession:
            async def __aenter__(self):
                self.context = db_session_factory()
                return await self.context.__aenter__()

            async def __aexit__(self, *args):
                nonlocal session_closed
                result = await self.context.__aexit__(*args)
                session_closed = True
                return result

        real_delete = cleaner._delete_workspace

        def assert_closed_then_delete(base, candidate_id):
            assert session_closed
            return real_delete(base, candidate_id)

        monkeypatch.setattr(cleaner, "_delete_workspace", assert_closed_then_delete)
        tracked_factory = cast(
            async_sessionmaker[AsyncSession], lambda: TrackedSession()
        )

        result = await purge_terminal_workspaces(
            tracked_factory, workspace_base, retention_seconds=3600
        )

        assert result.deleted == 1

    @pytest.mark.parametrize("link_kind", ["symlink", "symlink-broken", "junction"])
    async def test_internal_link_does_not_immortalize_expired_orphan(
        self, db_session_factory, workspace_base, tmp_path, link_kind
    ):
        """A .venv/node_modules-style internal link must not block orphan cleanup."""
        run_id = _run_id()
        path = _make_workspace(
            workspace_base, run_id, mtime=utcnow() - timedelta(days=30)
        )
        external_bin = tmp_path / "real-bin"
        external_bin.mkdir()
        marker = external_bin / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        bin_dir = path / ".venv" / "bin"
        bin_dir.mkdir(parents=True)
        try:
            if link_kind == "symlink":
                bin_dir.symlink_to(external_bin, target_is_directory=True)
            elif link_kind == "symlink-broken":
                bin_dir.symlink_to(
                    tmp_path / "missing-target", target_is_directory=True
                )
            else:
                _create_junction(external_bin, bin_dir)
        except (OSError, NotImplementedError, ImportError) as exc:
            pytest.skip(f"requested link kind unavailable: {exc}")

        result = await purge_terminal_workspaces(
            db_session_factory, workspace_base, retention_seconds=3600
        )

        assert result.candidates_found == 1
        assert result.deleted == 1
        assert result.errors == 0
        assert not path.exists()
        assert marker.exists()

    async def test_external_symlink_target_is_not_traversed_or_deleted(
        self, db_session_factory, workspace_base, tmp_path
    ):
        """Purging removes the workspace but never touches a linked target tree."""
        run_id = _run_id()
        outside = tmp_path / "outside-repo"
        (outside / "nested").mkdir(parents=True)
        marker = outside / "nested" / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        path = _make_workspace(workspace_base, run_id)
        try:
            (path / "repo-link").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"directory symlinks unavailable: {exc}")
        old_time = (utcnow() - timedelta(days=30)).timestamp()
        os.utime(path, (old_time, old_time))

        result = await purge_terminal_workspaces(
            db_session_factory, workspace_base, retention_seconds=3600
        )

        assert result.deleted == 1
        assert marker.exists()

    async def test_failed_deletions_stop_at_attempt_limit(
        self, db_session_factory, workspace_base, monkeypatch
    ):
        old = (utcnow() - timedelta(days=30)).timestamp()
        candidates = {uuid.uuid4(): old for _ in range(200)}
        delete = Mock(
            return_value=cleaner.WorkspaceDeleteResult(DeleteOutcome.ERROR, 0)
        )
        monkeypatch.setattr(cleaner, "_scan_candidates", lambda _root: candidates)
        monkeypatch.setattr(cleaner, "_delete_workspace", delete)

        result = await purge_terminal_workspaces(
            db_session_factory, workspace_base, retention_seconds=3600, batch_size=50
        )

        assert result.errors == 50 * DELETE_ATTEMPT_FACTOR
        assert delete.call_count == 50 * DELETE_ATTEMPT_FACTOR

    async def test_shutdown_stops_between_deletions(
        self, db_session_factory, workspace_base, monkeypatch
    ):
        old = (utcnow() - timedelta(days=30)).timestamp()
        candidates = {uuid.uuid4(): old for _ in range(2)}
        shutdown_event = asyncio.Event()
        delete = Mock()

        def stop_after_first(_base, _run_id):
            shutdown_event.set()
            return cleaner.WorkspaceDeleteResult(DeleteOutcome.ERROR, 0)

        delete.side_effect = stop_after_first
        monkeypatch.setattr(cleaner, "_scan_candidates", lambda _root: candidates)
        monkeypatch.setattr(cleaner, "_delete_workspace", delete)

        await purge_terminal_workspaces(
            db_session_factory,
            workspace_base,
            retention_seconds=3600,
            shutdown_event=shutdown_event,
        )

        assert delete.call_count == 1

    async def test_zero_retention_disables_direct_purge(
        self, db_session_factory, workspace_base
    ):
        path = _make_workspace(
            workspace_base, _run_id(), mtime=utcnow() - timedelta(days=30)
        )

        result = await purge_terminal_workspaces(
            db_session_factory, workspace_base, retention_seconds=0
        )

        assert result == PurgeResult(0, 0, 0, 0, 0, 0)
        assert path.exists()

    async def test_permanent_delete_error_does_not_starve_newer_workspace(
        self, db_session_factory, workspace_base, monkeypatch
    ):
        """A failed old candidate cannot consume the whole batch budget."""
        auto_id = uuid.uuid4()
        await _create_automation(db_session_factory, auto_id)
        old_time = utcnow() - timedelta(days=30)
        older_id = _run_id()
        newer_id = _run_id()
        await _create_run(
            db_session_factory,
            older_id,
            auto_id,
            AutomationRunStatus.COMPLETED,
            completed_at=old_time - timedelta(seconds=1),
        )
        await _create_run(
            db_session_factory,
            newer_id,
            auto_id,
            AutomationRunStatus.COMPLETED,
            completed_at=old_time,
        )
        older_path = _make_workspace(
            workspace_base,
            older_id,
            mtime=old_time - timedelta(seconds=1),
        )
        newer_path = _make_workspace(workspace_base, newer_id, mtime=old_time)
        real_rmtree = cleaner.shutil.rmtree

        def fail_old(path, *args, **kwargs):
            if Path(path) == older_path:
                raise PermissionError("locked")
            return real_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(cleaner.shutil, "rmtree", fail_old)

        first_result = await purge_terminal_workspaces(
            db_session_factory, workspace_base, retention_seconds=3600, batch_size=1
        )
        second_result = await purge_terminal_workspaces(
            db_session_factory, workspace_base, retention_seconds=3600, batch_size=1
        )

        assert first_result.errors == 1
        assert first_result.deleted == 1
        assert second_result.errors == 1
        assert second_result.deleted == 0
        assert older_path.exists()
        assert not newer_path.exists()

    async def test_vanished_candidate_is_treated_as_missing(
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
        real_delete = cleaner._delete_workspace

        def disappear_before_delete(base, candidate_id):
            if path.exists():
                real_rmtree = cleaner.shutil.rmtree
                real_rmtree(path)
            return real_delete(base, candidate_id)

        monkeypatch.setattr(cleaner, "_delete_workspace", disappear_before_delete)

        result = await purge_terminal_workspaces(
            db_session_factory, workspace_base, retention_seconds=3600, batch_size=1
        )

        assert result.candidates_found == 1
        assert result.missing == 1
        assert result.deleted == 0
        assert result.errors == 0

    async def test_terminal_run_without_completed_at_uses_workspace_mtime(
        self, db_session_factory, workspace_base
    ):
        """Missing completed_at falls back to the workspace mtime."""
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
        path = _make_workspace(
            workspace_base,
            run_id,
            mtime=utcnow() - timedelta(days=30),
        )

        result = await purge_terminal_workspaces(
            db_session_factory, workspace_base, retention_seconds=3600, batch_size=10
        )

        assert result.candidates_found == 1
        assert result.deleted == 1
        assert not path.exists()

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

        assert result.candidates_found == 1
        assert result.missing == 0
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
            return PurgeResult(0, 0, 0, 0, 0, 0)

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

    async def test_zero_retention_returns_without_purge(self, monkeypatch):
        mock_purge = AsyncMock()
        monkeypatch.setattr(cleaner, "purge_terminal_workspaces", mock_purge)

        await purger_loop(
            session_factory=AsyncMock(),
            workspace_base="/workspace",
            retention_seconds=0,
            interval_seconds=3600,
        )

        mock_purge.assert_not_awaited()


class TestPurgeLogging:
    async def test_successful_deletion_reports_run_and_bytes_at_info(
        self, db_session_factory, workspace_base, caplog
    ):
        """#271: operators must see what was removed and how much space it freed."""
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
        path = _make_workspace(workspace_base, run_id, "some data here")

        with caplog.at_level(logging.INFO, logger="automation.cleaner"):
            result = await purge_terminal_workspaces(
                db_session_factory, workspace_base, retention_seconds=3600
            )

        assert result.deleted == 1
        deletion_logs = [
            record
            for record in caplog.records
            if record.levelno == logging.INFO and str(run_id) in record.getMessage()
        ]
        assert any(str(result.bytes_freed) in rec.getMessage() for rec in deletion_logs)
        assert not path.exists()

    async def test_idle_cycle_does_not_log_info(
        self, db_session_factory, workspace_base, caplog
    ):
        """An hourly no-op cycle must not spam INFO forever."""
        _make_workspace(workspace_base, _run_id())

        with caplog.at_level(logging.INFO, logger="automation.cleaner"):
            result = await purge_terminal_workspaces(
                db_session_factory, workspace_base, retention_seconds=3600
            )

        assert result.deleted == 0
        assert not [
            record
            for record in caplog.records
            if record.name == "automation.cleaner" and record.levelno >= logging.INFO
        ]
