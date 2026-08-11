"""Integration tests for the git sync cycle (run_sync_cycle + mark_git_sync_dirty).

Uses an in-memory SQLite engine plus a real local bare git repo under
tmp_path, rather than mocking either.
"""

import asyncio
import io
import subprocess
import tarfile
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from openhands.automation.config import GitSyncSettings, ServiceSettings
from openhands.automation.git_sync import (
    git_sync_loop,
    is_git_sync_active,
    mark_git_sync_dirty,
    run_sync_cycle,
)
from openhands.automation.git_sync.client import (
    GitSyncError,
    commit_and_push,
    ensure_repo,
    pull,
)
from openhands.automation.git_sync.config_override import (
    apply_git_sync_config_override,
)
from openhands.automation.git_sync.loop import (
    GIT_SYNC_LAST_COMMIT_KEY,
    GIT_SYNC_LAST_ERROR_AT_KEY,
    GIT_SYNC_LAST_ERROR_KEY,
)
from openhands.automation.git_sync.serializer import encrypt_file_tree
from openhands.automation.models import (
    Automation,
    AutomationGitSyncState,
    Base,
    TarballUpload,
    UploadStatus,
)
from openhands.automation.storage.local import LocalFileStore
from openhands.automation.utils import utcnow
from openhands.automation.utils.service_metadata import (
    get_service_metadata,
    set_service_metadata,
)
from openhands.automation.utils.tarball_validation import parse_internal_upload_id


async def _aiter(data: bytes):
    yield data


def _make_tarball(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


@pytest.fixture
def origin(tmp_path):
    origin_dir = tmp_path / "origin"
    origin_dir.mkdir()
    subprocess.run(
        ["git", "init", "--bare", "-q", "-b", "main"], cwd=origin_dir, check=True
    )
    return origin_dir


@pytest.fixture
def file_store(tmp_path):
    return LocalFileStore(str(tmp_path / "filestore"))


@pytest.fixture
def git_settings(tmp_path, origin, monkeypatch):
    """Also sets matching env vars: mark_git_sync_dirty() reads the global
    cached config, not the settings objects returned here.
    """
    from openhands.automation.config import clear_config_cache

    monkeypatch.setenv("AUTOMATION_GIT_SYNC_ENABLED", "1")
    monkeypatch.setenv("AUTOMATION_GIT_SYNC_REPO_URL", f"file://{origin}")
    monkeypatch.setenv("AUTOMATION_GIT_SYNC_BRANCH", "main")
    monkeypatch.setenv("AUTOMATION_GIT_SYNC_PATH", "automations")
    monkeypatch.setenv("AUTOMATION_GIT_SYNC_LOCAL_WORKDIR", str(tmp_path / "workdir"))
    # The shipped default is 0 (manual-only). The git_sync_loop tests below
    # need an actual polling loop, so pin a short interval here; the
    # manual-only default gets its own test.
    monkeypatch.setenv("AUTOMATION_GIT_SYNC_INTERVAL_SECONDS", "1")
    monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", "http://localhost:3000")
    monkeypatch.setenv("AUTOMATION_LOCAL_API_KEY", "x")
    clear_config_cache()
    yield GitSyncSettings(
        git_sync_enabled=True,
        git_sync_repo_url=f"file://{origin}",
        git_sync_branch="main",
        git_sync_path="automations",
        git_sync_local_workdir=str(tmp_path / "workdir"),
    )
    clear_config_cache()


@pytest.fixture
def service_settings(git_settings):
    return ServiceSettings(agent_server_url="http://localhost:3000", local_api_key="x")


@pytest.fixture
async def sqlite_session_factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture(autouse=True)
def _patch_file_store(monkeypatch, file_store):
    """git_sync/loop.py resolves its own FileStore via get_file_store()."""
    monkeypatch.setattr(
        "openhands.automation.git_sync.loop.get_file_store", lambda: file_store
    )


async def _create_internal_automation(
    session_factory,
    file_store,
    *,
    name: str = "My First Automation",
    tarball_files: dict[str, bytes] | None = None,
) -> uuid.UUID:
    tarball_files = tarball_files or {"main.py": b"print(1)"}
    tarball_bytes = _make_tarball(tarball_files)
    upload_id = uuid.uuid4()
    storage_path = f"uploads/test/{upload_id}.tar"
    await file_store.write_stream(
        path=storage_path,
        stream=_aiter(tarball_bytes),
        content_type="application/x-tar",
    )

    async with session_factory() as session:
        user_id, org_id = uuid.uuid4(), uuid.uuid4()
        upload = TarballUpload(
            id=upload_id,
            user_id=user_id,
            org_id=org_id,
            name="t",
            status=UploadStatus.COMPLETED,
            storage_path=storage_path,
            size_bytes=len(tarball_bytes),
        )
        session.add(upload)
        automation = Automation(
            id=uuid.uuid4(),
            user_id=user_id,
            org_id=org_id,
            name=name,
            trigger={"type": "cron", "schedule": "0 9 * * 1"},
            tarball_path=f"oh-internal://uploads/{upload_id}",
            entrypoint="python main.py",
            enabled=True,
        )
        session.add(automation)
        await session.flush()
        await mark_git_sync_dirty(session, automation)
        await session.commit()
        return automation.id


class TestIsGitSyncActive:
    def test_inactive_without_local_mode(self, monkeypatch):
        monkeypatch.setenv("AUTOMATION_GIT_SYNC_ENABLED", "1")
        monkeypatch.setenv(
            "AUTOMATION_GIT_SYNC_REPO_URL", "https://example.com/repo.git"
        )
        from openhands.automation.config import clear_config_cache

        clear_config_cache()
        try:
            assert is_git_sync_active() is False
        finally:
            clear_config_cache()

    def test_active_with_local_mode_and_repo(self, monkeypatch):
        monkeypatch.setenv("AUTOMATION_GIT_SYNC_ENABLED", "1")
        monkeypatch.setenv(
            "AUTOMATION_GIT_SYNC_REPO_URL", "https://example.com/repo.git"
        )
        monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", "http://localhost:3000")
        from openhands.automation.config import clear_config_cache

        clear_config_cache()
        try:
            assert is_git_sync_active() is True
        finally:
            clear_config_cache()


class TestMarkGitSyncDirty:
    async def test_creates_dirty_state_row(
        self, sqlite_session_factory, file_store, git_settings
    ):
        automation_id = await _create_internal_automation(
            sqlite_session_factory, file_store
        )
        async with sqlite_session_factory() as session:
            state = await session.get(AutomationGitSyncState, automation_id)
            assert state is not None
            assert state.dirty is True
            assert state.slug == "my-first-automation"

    async def test_unexpected_failure_is_swallowed_and_does_not_lose_the_automation(
        self, sqlite_session_factory, git_settings, monkeypatch
    ):
        """An unexpected error inside mark_git_sync_dirty must not propagate:
        some callers treat any exception here as a failed automation
        creation. The automation row must survive and stay committable.
        """
        import openhands.automation.git_sync.loop as loop_module

        async def broken_inner(session, automation):
            raise RuntimeError("simulated unexpected DB error")

        monkeypatch.setattr(loop_module, "_mark_git_sync_dirty_inner", broken_inner)

        async with sqlite_session_factory() as session:
            automation = Automation(
                id=uuid.uuid4(),
                user_id=uuid.uuid4(),
                org_id=uuid.uuid4(),
                name="Survives",
                trigger={"type": "cron", "schedule": "0 9 * * 1"},
                tarball_path="oh-internal://uploads/" + str(uuid.uuid4()),
                entrypoint="python main.py",
                enabled=True,
            )
            session.add(automation)
            await session.flush()

            # Must not raise.
            await mark_git_sync_dirty(session, automation)

            await session.commit()
            automation_id = automation.id

        async with sqlite_session_factory() as session:
            persisted = await session.get(Automation, automation_id)
            assert persisted is not None
            assert persisted.name == "Survives"
            state = await session.get(AutomationGitSyncState, automation_id)
            assert state is None

    async def test_colliding_slug_falls_back_without_losing_either_automation(
        self, sqlite_session_factory, file_store, git_settings
    ):
        """Colliding slugs (same name) must not corrupt either automation row."""
        async with sqlite_session_factory() as session:
            user_id, org_id = uuid.uuid4(), uuid.uuid4()
            auto1 = Automation(
                id=uuid.uuid4(),
                user_id=user_id,
                org_id=org_id,
                name="New Automation",
                trigger={"type": "cron", "schedule": "0 9 * * 1"},
                tarball_path="oh-internal://uploads/" + str(uuid.uuid4()),
                entrypoint="python main.py",
                enabled=True,
            )
            auto2 = Automation(
                id=uuid.uuid4(),
                user_id=user_id,
                org_id=org_id,
                name="New Automation",
                trigger={"type": "cron", "schedule": "0 9 * * 1"},
                tarball_path="oh-internal://uploads/" + str(uuid.uuid4()),
                entrypoint="python main.py",
                enabled=True,
            )
            session.add(auto1)
            session.add(auto2)
            await session.flush()

            await mark_git_sync_dirty(session, auto1)
            await mark_git_sync_dirty(session, auto2)
            await session.commit()

        async with sqlite_session_factory() as session:
            automations = (await session.execute(select(Automation))).scalars().all()
            states = (
                (await session.execute(select(AutomationGitSyncState))).scalars().all()
            )
            assert len(automations) == 2
            assert len(states) == 2
            slugs = {s.slug for s in states}
            assert len(slugs) == 2
            assert "new-automation" in slugs


class TestRunSyncCycle:
    async def test_export_creates_commit_with_expected_layout(
        self, sqlite_session_factory, file_store, git_settings, service_settings, origin
    ):
        await _create_internal_automation(sqlite_session_factory, file_store)

        result = await run_sync_cycle(
            sqlite_session_factory, git_settings, service_settings
        )

        assert result.exported == 1
        assert result.pushed_commit is not None

        async with sqlite_session_factory() as session:
            states = (
                (await session.execute(select(AutomationGitSyncState))).scalars().all()
            )
            assert len(states) == 1
            assert states[0].dirty is False

        verify_dir = origin.parent / "verify"
        subprocess.run(
            ["git", "clone", f"file://{origin}", str(verify_dir)],
            check=True,
            capture_output=True,
        )
        slug_dir = verify_dir / "automations" / "my-first-automation"
        assert (slug_dir / "automation.yaml").is_file()
        assert (slug_dir / "tarball" / "main.py").read_text() == "print(1)"

    async def test_db_writes_are_committed_before_the_push_is_attempted(
        self,
        sqlite_session_factory,
        file_store,
        git_settings,
        service_settings,
        monkeypatch,
    ):
        """DB changes must be committed before commit_and_push runs, not
        held open across the (slow, network) push.
        """
        await _create_internal_automation(sqlite_session_factory, file_store)

        import openhands.automation.git_sync.loop as loop_module

        async def failing_commit_and_push(*args, **kwargs):
            raise GitSyncError("simulated push failure")

        monkeypatch.setattr(loop_module, "commit_and_push", failing_commit_and_push)

        with pytest.raises(GitSyncError):
            await run_sync_cycle(sqlite_session_factory, git_settings, service_settings)

        # The push failed, but the DB-side export bookkeeping must already
        # be durably committed (queryable from a fresh session) at this
        # point -- proving the DB transaction wasn't held open across the
        # (failing) push attempt.
        async with sqlite_session_factory() as session:
            states = (
                (await session.execute(select(AutomationGitSyncState))).scalars().all()
            )
            assert len(states) == 1
            assert states[0].dirty is False
            assert states[0].content_hash is not None

    async def test_second_cycle_is_a_noop(
        self, sqlite_session_factory, file_store, git_settings, service_settings
    ):
        await _create_internal_automation(sqlite_session_factory, file_store)
        await run_sync_cycle(sqlite_session_factory, git_settings, service_settings)

        result = await run_sync_cycle(
            sqlite_session_factory, git_settings, service_settings
        )

        assert result.exported == 0
        assert result.imported == 0
        assert result.pushed_commit is None

    async def test_changing_path_re_exports_under_the_new_path(
        self, sqlite_session_factory, file_store, git_settings, service_settings, origin
    ):
        """Changing `git_sync_path` must relocate the automations.

        Nothing about the automations changes, so none are dirty and the
        export step would write nothing under the new path -- it would stay
        empty while the old path silently kept the only copy. The old
        directory is intentionally left behind, so both must be present.
        """
        await _create_internal_automation(sqlite_session_factory, file_store)
        await run_sync_cycle(sqlite_session_factory, git_settings, service_settings)

        moved = git_settings.model_copy(update={"git_sync_path": "custom-path"})
        result = await run_sync_cycle(sqlite_session_factory, moved, service_settings)

        assert result.exported == 1
        assert result.pushed_commit is not None
        assert result.deleted_in_db == 0, "a path change must not delete anything"

        verify_dir = origin.parent / "verify_moved_path"
        subprocess.run(
            ["git", "clone", f"file://{origin}", str(verify_dir)],
            check=True,
            capture_output=True,
        )
        moved_dir = verify_dir / "custom-path" / "my-first-automation"
        assert (moved_dir / "automation.yaml").is_file()
        assert (moved_dir / "tarball" / "main.py").read_text() == "print(1)"
        # Left in place on purpose; the cycle warns about it instead.
        assert (
            verify_dir / "automations" / "my-first-automation" / "automation.yaml"
        ).is_file()

    async def test_changing_path_does_not_soft_delete_automations(
        self, sqlite_session_factory, file_store, git_settings, service_settings
    ):
        """A path change must never be read as a deletion.

        The import runs before the export and scans the new, still-empty
        path. With an unreachable base commit it can't diff and falls back
        to a full scan, which drops the "untouched by this range" guard --
        leaving its "directory disappeared" branch free to soft-delete every
        automation at once. Only the dirty flag stops it.
        """
        automation_id = await _create_internal_automation(
            sqlite_session_factory, file_store
        )
        await run_sync_cycle(sqlite_session_factory, git_settings, service_settings)

        # Unreachable base -> diff fails -> full-scan fallback.
        async with sqlite_session_factory() as session:
            await set_service_metadata(session, GIT_SYNC_LAST_COMMIT_KEY, "0" * 40)
            await session.commit()

        moved = git_settings.model_copy(update={"git_sync_path": "elsewhere"})
        result = await run_sync_cycle(sqlite_session_factory, moved, service_settings)

        assert result.deleted_in_db == 0
        async with sqlite_session_factory() as session:
            automation = await session.get(Automation, automation_id)
            assert automation is not None
            assert automation.deleted_at is None
            assert automation.enabled is True

    async def test_repointing_repo_url_pushes_without_newly_dirty_work(
        self,
        sqlite_session_factory,
        file_store,
        git_settings,
        service_settings,
        tmp_path,
    ):
        """Changing `git_sync_repo_url` must carry already-exported
        automations over to the new remote.

        By this point nothing is dirty, so a cycle that only pushes when it
        exported something this round would report success while leaving the
        new remote permanently empty -- the failure mode is silence, not an
        error. Same shape as a push that failed once and was followed by no
        further DB changes.
        """
        await _create_internal_automation(sqlite_session_factory, file_store)
        first = await run_sync_cycle(
            sqlite_session_factory, git_settings, service_settings
        )
        assert first.pushed_commit is not None

        new_origin = tmp_path / "new_origin"
        new_origin.mkdir()
        subprocess.run(
            ["git", "init", "--bare", "-q", "-b", "main"], cwd=new_origin, check=True
        )
        repointed = git_settings.model_copy(
            update={"git_sync_repo_url": f"file://{new_origin}"}
        )

        result = await run_sync_cycle(
            sqlite_session_factory, repointed, service_settings
        )

        assert result.exported == 0, "nothing should be newly dirty"
        assert result.pushed_commit is not None, (
            "the existing commit never reached the repointed remote"
        )

        new_origin_head = subprocess.run(
            ["git", "rev-parse", "main"],
            cwd=new_origin,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert new_origin_head == result.pushed_commit

        verify_dir = tmp_path / "verify_new_origin"
        subprocess.run(
            ["git", "clone", f"file://{new_origin}", str(verify_dir)],
            check=True,
            capture_output=True,
        )
        slug_dir = verify_dir / "automations" / "my-first-automation"
        assert (slug_dir / "automation.yaml").is_file()
        assert (slug_dir / "tarball" / "main.py").read_text() == "print(1)"

    async def test_missing_tarball_storage_preserves_previously_synced_content(
        self, sqlite_session_factory, file_store, git_settings, service_settings, origin
    ):
        """A missing tarball must not overwrite already-synced git content --
        the export should be skipped (automation stays dirty), not silently
        replaced with an empty/metadata-only export.
        """
        automation_id = await _create_internal_automation(
            sqlite_session_factory, file_store
        )
        result1 = await run_sync_cycle(
            sqlite_session_factory, git_settings, service_settings
        )
        assert result1.exported == 1

        async with sqlite_session_factory() as session:
            automation = await session.get(Automation, automation_id)
            upload_id = parse_internal_upload_id(automation.tarball_path)
            upload = await session.get(TarballUpload, upload_id)
            file_store.delete(upload.storage_path)

            # Mark dirty via an unrelated field edit, mirroring a normal API
            # update -- the tarball itself was never touched by this edit.
            automation.timeout = 999
            await mark_git_sync_dirty(session, automation)
            await session.commit()

        result2 = await run_sync_cycle(
            sqlite_session_factory, git_settings, service_settings
        )

        assert result2.exported == 0
        assert result2.pushed_commit is None

        async with sqlite_session_factory() as session:
            state = await session.get(AutomationGitSyncState, automation_id)
            assert state.dirty is True

        verify_dir = origin.parent / "verify_missing_tarball"
        subprocess.run(
            ["git", "clone", f"file://{origin}", str(verify_dir)],
            check=True,
            capture_output=True,
        )
        slug_dir = verify_dir / "automations" / "my-first-automation"
        assert (slug_dir / "tarball" / "main.py").read_text() == "print(1)"

    async def test_git_side_edit_is_imported(
        self, sqlite_session_factory, file_store, git_settings, service_settings, origin
    ):
        automation_id = await _create_internal_automation(
            sqlite_session_factory, file_store
        )
        await run_sync_cycle(sqlite_session_factory, git_settings, service_settings)

        editor_dir = origin.parent / "editor"
        await ensure_repo(editor_dir, f"file://{origin}", "main", "", 30)
        await pull(editor_dir, "main", "", 30)
        yaml_path = (
            editor_dir / "automations" / "my-first-automation" / "automation.yaml"
        )
        yaml_path.write_text(
            yaml_path.read_text().replace(
                "entrypoint: python main.py", "entrypoint: python main.py --flag"
            )
        )
        await commit_and_push(
            editor_dir,
            "automations",
            "edit",
            "Human",
            "human@example.com",
            "main",
            "",
            30,
        )

        result = await run_sync_cycle(
            sqlite_session_factory, git_settings, service_settings
        )
        assert result.imported == 0  # matched an existing slug, not a new automation

        async with sqlite_session_factory() as session:
            automation = await session.get(Automation, automation_id)
            assert automation.entrypoint == "python main.py --flag"

    async def test_import_only_reads_directories_that_actually_changed(
        self,
        sqlite_session_factory,
        file_store,
        git_settings,
        service_settings,
        origin,
        monkeypatch,
    ):
        """A commit touching one automation must not re-read every other
        tracked automation's tarball tree too.
        """
        await _create_internal_automation(
            sqlite_session_factory, file_store, name="Automation A"
        )
        await _create_internal_automation(
            sqlite_session_factory, file_store, name="Automation B"
        )
        await run_sync_cycle(sqlite_session_factory, git_settings, service_settings)

        editor_dir = origin.parent / "editor"
        await ensure_repo(editor_dir, f"file://{origin}", "main", "", 30)
        await pull(editor_dir, "main", "", 30)
        yaml_path = editor_dir / "automations" / "automation-b" / "automation.yaml"
        yaml_path.write_text(
            yaml_path.read_text().replace(
                "entrypoint: python main.py", "entrypoint: python main.py --flag"
            )
        )
        await commit_and_push(
            editor_dir,
            "automations",
            "edit b",
            "Human",
            "human@example.com",
            "main",
            "",
            30,
        )

        import openhands.automation.git_sync.loop as loop_module

        read_slugs = []
        real_read = loop_module._read_directory_files

        def instrumented_read(directory):
            read_slugs.append(directory.name)
            return real_read(directory)

        monkeypatch.setattr(loop_module, "_read_directory_files", instrumented_read)

        await run_sync_cycle(sqlite_session_factory, git_settings, service_settings)

        assert read_slugs == ["automation-b"]

    async def test_dirty_automation_wins_over_conflicting_git_edit(
        self, sqlite_session_factory, file_store, git_settings, service_settings, origin
    ):
        automation_id = await _create_internal_automation(
            sqlite_session_factory, file_store
        )
        await run_sync_cycle(sqlite_session_factory, git_settings, service_settings)

        # Git-side edit, not yet synced back.
        editor_dir = origin.parent / "editor"
        await ensure_repo(editor_dir, f"file://{origin}", "main", "", 30)
        await pull(editor_dir, "main", "", 30)
        yaml_path = (
            editor_dir / "automations" / "my-first-automation" / "automation.yaml"
        )
        yaml_path.write_text(
            yaml_path.read_text().replace(
                "entrypoint: python main.py", "entrypoint: python main.py --from-git"
            )
        )
        await commit_and_push(
            editor_dir,
            "automations",
            "git edit",
            "Human",
            "human@example.com",
            "main",
            "",
            30,
        )

        # VM-side edit via the API, marking the automation dirty again.
        async with sqlite_session_factory() as session:
            automation = await session.get(Automation, automation_id)
            automation.entrypoint = "python main.py --from-vm"
            await mark_git_sync_dirty(session, automation)
            await session.commit()

        await run_sync_cycle(sqlite_session_factory, git_settings, service_settings)

        async with sqlite_session_factory() as session:
            automation = await session.get(Automation, automation_id)
            assert automation.entrypoint == "python main.py --from-vm"

    async def test_delete_via_api_removes_directory_from_git(
        self, sqlite_session_factory, file_store, git_settings, service_settings, origin
    ):
        automation_id = await _create_internal_automation(
            sqlite_session_factory, file_store
        )
        await run_sync_cycle(sqlite_session_factory, git_settings, service_settings)

        async with sqlite_session_factory() as session:
            automation = await session.get(Automation, automation_id)
            automation.enabled = False
            automation.deleted_at = utcnow()
            await mark_git_sync_dirty(session, automation)
            await session.commit()

        result = await run_sync_cycle(
            sqlite_session_factory, git_settings, service_settings
        )
        assert result.deleted_in_git == 1

        async with sqlite_session_factory() as session:
            remaining = (
                (await session.execute(select(AutomationGitSyncState))).scalars().all()
            )
            assert remaining == []

        verify_dir = origin.parent / "verify2"
        subprocess.run(
            ["git", "clone", f"file://{origin}", str(verify_dir)],
            check=True,
            capture_output=True,
        )
        assert not (verify_dir / "automations" / "my-first-automation").exists()

    async def test_deleting_directory_in_git_soft_deletes_automation(
        self, sqlite_session_factory, file_store, git_settings, service_settings, origin
    ):
        automation_id = await _create_internal_automation(
            sqlite_session_factory, file_store
        )
        await run_sync_cycle(sqlite_session_factory, git_settings, service_settings)

        editor_dir = origin.parent / "editor"
        await ensure_repo(editor_dir, f"file://{origin}", "main", "", 30)
        await pull(editor_dir, "main", "", 30)
        subprocess.run(
            ["git", "rm", "-r", "automations/my-first-automation"],
            cwd=editor_dir,
            check=True,
            capture_output=True,
        )
        await commit_and_push(
            editor_dir,
            "automations",
            "remove",
            "Human",
            "human@example.com",
            "main",
            "",
            30,
        )

        result = await run_sync_cycle(
            sqlite_session_factory, git_settings, service_settings
        )
        assert result.deleted_in_db == 1

        async with sqlite_session_factory() as session:
            automation = await session.get(Automation, automation_id)
            assert automation.deleted_at is not None
            assert automation.enabled is False

    async def test_new_automation_authored_directly_in_git_is_imported(
        self, sqlite_session_factory, git_settings, service_settings, origin
    ):
        seed_dir = origin.parent / "seed"
        await ensure_repo(seed_dir, f"file://{origin}", "main", "", 30)
        await pull(seed_dir, "main", "", 30)
        autodir = seed_dir / "automations" / "hand-written"
        autodir.mkdir(parents=True)
        (autodir / "automation.yaml").write_text(
            "name: Hand Written\n"
            "entrypoint: python main.py\n"
            "trigger:\n"
            "  type: cron\n"
            "  schedule: '0 12 * * *'\n"
            "  timezone: UTC\n"
            "enabled: true\n"
        )
        (autodir / "tarball").mkdir()
        (autodir / "tarball" / "main.py").write_text('print("hi")')
        await commit_and_push(
            seed_dir,
            "automations",
            "seed",
            "Human",
            "human@example.com",
            "main",
            "",
            30,
        )

        result = await run_sync_cycle(
            sqlite_session_factory, git_settings, service_settings
        )
        assert result.imported == 1

        async with sqlite_session_factory() as session:
            automations = (await session.execute(select(Automation))).scalars().all()
            assert len(automations) == 1
            assert automations[0].name == "Hand Written"
            assert automations[0].entrypoint == "python main.py"

    async def test_symlinked_tarball_member_does_not_leak_host_files(
        self, sqlite_session_factory, file_store, git_settings, service_settings, origin
    ):
        """A symlink in a synced commit must not be followed into the
        resulting automation's tarball (could leak host files)."""
        secret_path = origin.parent / "secret.txt"
        secret_path.write_text("TOP SECRET HOST FILE")

        seed_dir = origin.parent / "seed"
        await ensure_repo(seed_dir, f"file://{origin}", "main", "", 30)
        await pull(seed_dir, "main", "", 30)
        autodir = seed_dir / "automations" / "evil"
        (autodir / "tarball").mkdir(parents=True)
        (autodir / "automation.yaml").write_text(
            "name: Evil\n"
            "entrypoint: python main.py\n"
            "trigger: {type: cron, schedule: '0 9 * * *'}\n"
            "enabled: true\n"
        )
        (autodir / "tarball" / "main.py").write_text("print(1)")
        (autodir / "tarball" / "leaked.txt").symlink_to(secret_path)
        await commit_and_push(
            seed_dir,
            "automations",
            "evil commit",
            "Human",
            "human@example.com",
            "main",
            "",
            30,
        )

        result = await run_sync_cycle(
            sqlite_session_factory, git_settings, service_settings
        )
        assert result.imported == 1

        async with sqlite_session_factory() as session:
            automation = (await session.execute(select(Automation))).scalars().first()
            upload_id = parse_internal_upload_id(automation.tarball_path)
            upload = await session.get(TarballUpload, upload_id)
            tarball_bytes = file_store.read(upload.storage_path)
            with tarfile.open(fileobj=io.BytesIO(tarball_bytes)) as tar:
                names = tar.getnames()
                assert "leaked.txt" not in names
                for name in names:
                    extracted = tar.extractfile(name)
                    assert extracted is not None
                    assert b"TOP SECRET" not in extracted.read()

    async def test_invalid_git_directory_is_skipped_not_fatal(
        self, sqlite_session_factory, git_settings, service_settings, origin
    ):
        seed_dir = origin.parent / "seed"
        await ensure_repo(seed_dir, f"file://{origin}", "main", "", 30)
        await pull(seed_dir, "main", "", 30)
        autodir = seed_dir / "automations" / "broken"
        autodir.mkdir(parents=True)
        # Missing required 'entrypoint'.
        (autodir / "automation.yaml").write_text(
            "name: Broken\ntrigger: {type: cron, schedule: '0 9 * * *'}\n"
        )
        await commit_and_push(
            seed_dir,
            "automations",
            "seed broken",
            "Human",
            "human@example.com",
            "main",
            "",
            30,
        )

        result = await run_sync_cycle(
            sqlite_session_factory, git_settings, service_settings
        )

        assert result.imported == 0
        async with sqlite_session_factory() as session:
            automations = (await session.execute(select(Automation))).scalars().all()
            assert automations == []

    async def test_concurrent_cycles_are_serialized(
        self, sqlite_session_factory, git_settings, service_settings, monkeypatch
    ):
        """A manually triggered cycle must not run concurrently with the
        periodic loop's cycle against the same workdir."""
        import openhands.automation.git_sync.loop as loop_module

        concurrency = {"active": 0, "max": 0}
        real_ensure_repo = loop_module.ensure_repo

        async def instrumented_ensure_repo(*args, **kwargs):
            concurrency["active"] += 1
            concurrency["max"] = max(concurrency["max"], concurrency["active"])
            await asyncio.sleep(0.05)
            try:
                return await real_ensure_repo(*args, **kwargs)
            finally:
                concurrency["active"] -= 1

        monkeypatch.setattr(loop_module, "ensure_repo", instrumented_ensure_repo)

        await asyncio.gather(
            run_sync_cycle(sqlite_session_factory, git_settings, service_settings),
            run_sync_cycle(sqlite_session_factory, git_settings, service_settings),
        )

        assert concurrency["max"] == 1


class TestEncryption:
    async def test_export_is_opaque_and_stable_across_cycles(
        self, sqlite_session_factory, file_store, git_settings, service_settings, origin
    ):
        encrypted_settings = git_settings.model_copy(
            update={"git_sync_encryption_key": "super-secret-key"}
        )
        await _create_internal_automation(
            sqlite_session_factory, file_store, name="Secret Automation"
        )

        first = await run_sync_cycle(
            sqlite_session_factory, encrypted_settings, service_settings
        )
        assert first.exported == 1
        assert first.pushed_commit is not None

        verify_dir = origin.parent / "verify"
        subprocess.run(
            ["git", "clone", f"file://{origin}", str(verify_dir)],
            check=True,
            capture_output=True,
        )
        slug_dir = verify_dir / "automations" / "secret-automation"
        yaml_bytes = (slug_dir / "automation.yaml").read_bytes()
        tarball_bytes = (slug_dir / "tarball" / "main.py").read_bytes()
        assert b"entrypoint" not in yaml_bytes
        assert b"print(1)" not in tarball_bytes

        # A second cycle with nothing new to export must not touch the repo
        # again -- Fernet's random per-encryption IV must not make identical
        # content look "changed" and trigger a needless commit.
        second = await run_sync_cycle(
            sqlite_session_factory, encrypted_settings, service_settings
        )
        assert second.exported == 0
        assert second.pushed_commit is None

    async def test_encrypted_automation_authored_in_git_is_imported(
        self, sqlite_session_factory, git_settings, service_settings, origin
    ):
        encrypted_settings = git_settings.model_copy(
            update={"git_sync_encryption_key": "super-secret-key"}
        )
        plaintext_files = {
            "automation.yaml": (
                b"name: Hand Written\n"
                b"entrypoint: python main.py\n"
                b"trigger:\n"
                b"  type: cron\n"
                b"  schedule: '0 12 * * *'\n"
                b"  timezone: UTC\n"
                b"enabled: true\n"
            ),
            "tarball/main.py": b'print("hi")',
        }
        encrypted_files = encrypt_file_tree(plaintext_files, "super-secret-key")

        seed_dir = origin.parent / "seed"
        await ensure_repo(seed_dir, f"file://{origin}", "main", "", 30)
        await pull(seed_dir, "main", "", 30)
        autodir = seed_dir / "automations" / "hand-written"
        (autodir / "tarball").mkdir(parents=True)
        (autodir / "automation.yaml").write_bytes(encrypted_files["automation.yaml"])
        (autodir / "tarball" / "main.py").write_bytes(
            encrypted_files["tarball/main.py"]
        )
        await commit_and_push(
            seed_dir,
            "automations",
            "seed",
            "Human",
            "human@example.com",
            "main",
            "",
            30,
        )

        result = await run_sync_cycle(
            sqlite_session_factory, encrypted_settings, service_settings
        )
        assert result.imported == 1

        async with sqlite_session_factory() as session:
            automations = (await session.execute(select(Automation))).scalars().all()
            assert len(automations) == 1
            assert automations[0].name == "Hand Written"
            assert automations[0].entrypoint == "python main.py"

    async def test_disabling_encryption_skips_leftover_ciphertext_instead_of_crashing(
        self,
        sqlite_session_factory,
        file_store,
        git_settings,
        service_settings,
        origin,
        tmp_path,
    ):
        """A repo can have leftover ciphertext from when encryption was
        previously on (e.g. a different deployment, or a key later turned
        off). A fresh sync target's first cycle does a full-directory scan
        (no last_commit recorded yet) and must skip that directory, not
        crash the whole cycle."""
        encrypted_settings = git_settings.model_copy(
            update={"git_sync_encryption_key": "the-old-key"}
        )
        await _create_internal_automation(
            sqlite_session_factory, file_store, name="Was Encrypted"
        )
        await run_sync_cycle(
            sqlite_session_factory, encrypted_settings, service_settings
        )

        # A fresh DB pointed at the same repo, no encryption key configured:
        # its first cycle has no last_commit yet, so it full-scans every
        # directory -- including the leftover ciphertext one.
        fresh_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/fresh.db")
        async with fresh_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        fresh_factory = async_sessionmaker(
            fresh_engine, class_=AsyncSession, expire_on_commit=False
        )
        fresh_workdir_settings = git_settings.model_copy(
            update={"git_sync_local_workdir": str(tmp_path / "fresh-workdir")}
        )
        try:
            result = await run_sync_cycle(
                fresh_factory, fresh_workdir_settings, service_settings
            )
            assert result.imported == 0  # skipped, not crashed
        finally:
            await fresh_engine.dispose()

    async def test_wrong_key_skips_directory_instead_of_crashing(
        self, sqlite_session_factory, git_settings, service_settings, origin
    ):
        plaintext_files = {
            "automation.yaml": (
                b"name: Hand Written\n"
                b"entrypoint: python main.py\n"
                b"trigger: {type: cron, schedule: '0 9 * * *'}\n"
                b"enabled: true\n"
            ),
        }
        encrypted_files = encrypt_file_tree(plaintext_files, "the-real-key")

        seed_dir = origin.parent / "seed"
        await ensure_repo(seed_dir, f"file://{origin}", "main", "", 30)
        await pull(seed_dir, "main", "", 30)
        autodir = seed_dir / "automations" / "hand-written"
        autodir.mkdir(parents=True)
        (autodir / "automation.yaml").write_bytes(encrypted_files["automation.yaml"])
        await commit_and_push(
            seed_dir,
            "automations",
            "seed",
            "Human",
            "human@example.com",
            "main",
            "",
            30,
        )

        wrong_key_settings = git_settings.model_copy(
            update={"git_sync_encryption_key": "the-wrong-key"}
        )
        result = await run_sync_cycle(
            sqlite_session_factory, wrong_key_settings, service_settings
        )

        assert result.imported == 0
        async with sqlite_session_factory() as session:
            automations = (await session.execute(select(Automation))).scalars().all()
            assert automations == []


class TestLastError:
    async def test_failed_cycle_records_last_error(
        self, sqlite_session_factory, git_settings, service_settings, monkeypatch
    ):
        import openhands.automation.git_sync.loop as loop_module

        async def failing_pull(*args, **kwargs):
            raise GitSyncError("simulated pull failure")

        monkeypatch.setattr(loop_module, "pull", failing_pull)

        with pytest.raises(GitSyncError):
            await run_sync_cycle(sqlite_session_factory, git_settings, service_settings)

        async with sqlite_session_factory() as session:
            last_error = await get_service_metadata(session, GIT_SYNC_LAST_ERROR_KEY)
            last_error_at = await get_service_metadata(
                session, GIT_SYNC_LAST_ERROR_AT_KEY
            )
            assert last_error is not None
            assert "simulated pull failure" in last_error
            assert last_error_at is not None

    async def test_successful_cycle_clears_previous_error(
        self, sqlite_session_factory, file_store, git_settings, service_settings
    ):
        async with sqlite_session_factory() as session:
            await set_service_metadata(
                session, GIT_SYNC_LAST_ERROR_KEY, "a stale error"
            )
            await set_service_metadata(
                session, GIT_SYNC_LAST_ERROR_AT_KEY, utcnow().isoformat()
            )
            await session.commit()

        await _create_internal_automation(sqlite_session_factory, file_store)
        await run_sync_cycle(sqlite_session_factory, git_settings, service_settings)

        async with sqlite_session_factory() as session:
            last_error = await get_service_metadata(session, GIT_SYNC_LAST_ERROR_KEY)
            last_error_at = await get_service_metadata(
                session, GIT_SYNC_LAST_ERROR_AT_KEY
            )
            assert last_error == ""
            # Both halves, or /status reports a timestamp with no message.
            assert not last_error_at


class TestGitSyncLoop:
    """Covers git_sync_loop() itself, not just run_sync_cycle() -- the loop
    is what actually re-resolves runtime config overrides every tick and
    skips a cycle while paused, and had no coverage of its own before.
    """

    async def test_runs_a_cycle_and_exits_on_shutdown(
        self, sqlite_session_factory, file_store, git_settings, service_settings
    ):
        await _create_internal_automation(sqlite_session_factory, file_store)
        shutdown_event = asyncio.Event()

        async def stop_once_synced():
            async with sqlite_session_factory() as session:
                for _ in range(200):
                    states = (
                        (await session.execute(select(AutomationGitSyncState)))
                        .scalars()
                        .all()
                    )
                    if states and not states[0].dirty:
                        break
                    await asyncio.sleep(0.02)
            shutdown_event.set()

        await asyncio.wait_for(
            asyncio.gather(
                git_sync_loop(sqlite_session_factory, shutdown_event=shutdown_event),
                stop_once_synced(),
            ),
            timeout=15,
        )

        async with sqlite_session_factory() as session:
            states = (
                (await session.execute(select(AutomationGitSyncState))).scalars().all()
            )
            assert states[0].dirty is False
            assert states[0].content_hash is not None

    async def test_manual_only_by_default_starts_no_polling_loop(
        self, sqlite_session_factory, file_store, git_settings, monkeypatch
    ):
        """The shipped default is manual-only: sync happens when
        POST /v1/git-sync/sync is called, not on a timer. git_sync_loop must
        return immediately rather than polling (or busy-spinning on a zero
        sleep) so nothing is pushed behind the user's back.
        """
        from openhands.automation.config import clear_config_cache

        monkeypatch.delenv("AUTOMATION_GIT_SYNC_INTERVAL_SECONDS", raising=False)
        clear_config_cache()
        await _create_internal_automation(sqlite_session_factory, file_store)

        # No shutdown_event: a loop that polls would hang here.
        await asyncio.wait_for(git_sync_loop(sqlite_session_factory), timeout=5)

        async with sqlite_session_factory() as session:
            states = (
                (await session.execute(select(AutomationGitSyncState))).scalars().all()
            )
            assert states[0].dirty is True, "manual-only must not have synced"

    async def test_paused_via_override_does_not_run_a_cycle(
        self, sqlite_session_factory, file_store, git_settings, service_settings
    ):
        async with sqlite_session_factory() as session:
            await apply_git_sync_config_override(session, {"git_sync_enabled": False})
            await session.commit()

        await _create_internal_automation(sqlite_session_factory, file_store)
        shutdown_event = asyncio.Event()

        async def stop_soon():
            await asyncio.sleep(0.3)
            shutdown_event.set()

        await asyncio.wait_for(
            asyncio.gather(
                git_sync_loop(sqlite_session_factory, shutdown_event=shutdown_event),
                stop_soon(),
            ),
            timeout=15,
        )

        async with sqlite_session_factory() as session:
            states = (
                (await session.execute(select(AutomationGitSyncState))).scalars().all()
            )
            # Still dirty -- the loop resolved the paused override and
            # skipped the cycle instead of exporting.
            assert states[0].dirty is True
