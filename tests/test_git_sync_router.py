"""Tests for the git sync status/trigger API endpoints."""

import asyncio
import subprocess

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from openhands.automation.config import (
    GitSyncSettings,
    ServiceSettings,
    clear_config_cache,
)
from openhands.automation.git_sync.router import _background_sync_tasks


@pytest.fixture(autouse=True)
def writable_workspace(tmp_path, monkeypatch):
    """Point `workspace_base` at a temp dir for every test in this module.

    Storing a git-sync secret provisions a wrapping key under the workspace
    (see secret_store.py), and the default "/workspace" is not writable
    outside a container -- nor should a test write there.
    """
    monkeypatch.setenv("AUTOMATION_WORKSPACE_BASE", str(tmp_path))
    clear_config_cache()
    yield
    clear_config_cache()


class TestGitSyncStatus:
    async def test_disabled_by_default(self, async_client):
        response = await async_client.get("/api/automation/v1/git-sync/status")

        assert response.status_code == 200
        body = response.json()
        assert body["enabled"] is False
        assert body["dirty_count"] == 0
        assert body["last_synced_commit"] is None
        assert body["last_synced_at"] is None

    async def test_requires_manage_automations_permission(self, readonly_client):
        response = await readonly_client.get("/api/automation/v1/git-sync/status")
        assert response.status_code == 403

    async def test_reflects_configured_repo(self, async_client, monkeypatch):
        monkeypatch.setenv("AUTOMATION_GIT_SYNC_ENABLED", "1")
        monkeypatch.setenv(
            "AUTOMATION_GIT_SYNC_REPO_URL", "https://example.com/repo.git"
        )
        monkeypatch.setenv("AUTOMATION_GIT_SYNC_BRANCH", "release")
        monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", "http://localhost:3000")
        clear_config_cache()
        try:
            response = await async_client.get("/api/automation/v1/git-sync/status")
        finally:
            clear_config_cache()

        assert response.status_code == 200
        body = response.json()
        assert body["enabled"] is True
        assert body["repo_url"] == "https://example.com/repo.git"
        assert body["branch"] == "release"


class TestGitSyncConfig:
    async def test_requires_manage_automations_permission(self, readonly_client):
        response = await readonly_client.put(
            "/api/automation/v1/git-sync/config", json={"branch": "develop"}
        )
        assert response.status_code == 403

    async def test_partial_update_persists_and_reflects_in_status(self, async_client):
        put_response = await async_client.put(
            "/api/automation/v1/git-sync/config",
            json={"branch": "develop", "encryption_key": "the-key"},
        )
        assert put_response.status_code == 200
        assert put_response.json()["branch"] == "develop"
        assert put_response.json()["encryption_enabled"] is True

        status_response = await async_client.get("/api/automation/v1/git-sync/status")
        body = status_response.json()
        assert body["branch"] == "develop"
        assert body["encryption_enabled"] is True
        # The key itself is never echoed back in any response.
        assert "encryption_key" not in body
        assert "token" not in body

    async def test_omitted_fields_are_left_unchanged(self, async_client):
        await async_client.put(
            "/api/automation/v1/git-sync/config", json={"branch": "develop"}
        )
        response = await async_client.put(
            "/api/automation/v1/git-sync/config", json={"path": "custom-path"}
        )
        body = response.json()
        assert body["branch"] == "develop"
        assert body["path"] == "custom-path"

    async def test_null_clears_override_back_to_env_default(
        self, async_client, monkeypatch
    ):
        monkeypatch.setenv("AUTOMATION_GIT_SYNC_BRANCH", "main")
        clear_config_cache()
        try:
            await async_client.put(
                "/api/automation/v1/git-sync/config", json={"branch": "develop"}
            )
            response = await async_client.put(
                "/api/automation/v1/git-sync/config", json={"branch": None}
            )
        finally:
            clear_config_cache()
        assert response.json()["branch"] == "main"

    async def test_pause_via_override_503s_the_manual_trigger(
        self, async_client, monkeypatch
    ):
        monkeypatch.setenv("AUTOMATION_GIT_SYNC_ENABLED", "1")
        monkeypatch.setenv(
            "AUTOMATION_GIT_SYNC_REPO_URL", "https://example.com/repo.git"
        )
        monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", "http://localhost:3000")
        clear_config_cache()
        try:
            enabled = await async_client.post("/api/automation/v1/git-sync/sync")
            assert enabled.status_code == 202

            await async_client.put(
                "/api/automation/v1/git-sync/config", json={"enabled": False}
            )
            paused = await async_client.post("/api/automation/v1/git-sync/sync")
            assert paused.status_code == 503

            await async_client.put(
                "/api/automation/v1/git-sync/config", json={"enabled": None}
            )
            resumed = await async_client.post("/api/automation/v1/git-sync/sync")
            assert resumed.status_code == 202
        finally:
            clear_config_cache()


class TestGitSyncInterval:
    async def test_defaults_to_manual_only(self, async_client):
        response = await async_client.get("/api/automation/v1/git-sync/status")
        assert response.json()["interval_seconds"] == 0

    async def test_set_and_cleared_via_config(self, async_client):
        put = await async_client.put(
            "/api/automation/v1/git-sync/config", json={"interval_seconds": 300}
        )
        assert put.status_code == 200
        assert put.json()["interval_seconds"] == 300

        status = await async_client.get("/api/automation/v1/git-sync/status")
        assert status.json()["interval_seconds"] == 300

        # null clears the override, back to manual-only.
        cleared = await async_client.put(
            "/api/automation/v1/git-sync/config", json={"interval_seconds": None}
        )
        assert cleared.json()["interval_seconds"] == 0

    async def test_rejects_a_negative_interval(self, async_client):
        response = await async_client.put(
            "/api/automation/v1/git-sync/config", json={"interval_seconds": -1}
        )
        assert response.status_code == 422

    async def test_setting_interval_leaves_other_fields_alone(self, async_client):
        await async_client.put(
            "/api/automation/v1/git-sync/config", json={"branch": "develop"}
        )
        response = await async_client.put(
            "/api/automation/v1/git-sync/config", json={"interval_seconds": 60}
        )
        body = response.json()
        assert body["branch"] == "develop"
        assert body["interval_seconds"] == 60


class TestTriggerGitSync:
    async def test_returns_503_when_disabled(self, async_client):
        response = await async_client.post("/api/automation/v1/git-sync/sync")
        assert response.status_code == 503

    async def test_requires_manage_automations_permission(self, readonly_client):
        # Permission dependency resolves before the handler body runs, so
        # this 403s even though git sync isn't enabled in this test.
        response = await readonly_client.post("/api/automation/v1/git-sync/sync")
        assert response.status_code == 403

    async def test_returns_202_and_schedules_when_enabled(
        self, async_client, monkeypatch
    ):
        monkeypatch.setenv("AUTOMATION_GIT_SYNC_ENABLED", "1")
        monkeypatch.setenv(
            "AUTOMATION_GIT_SYNC_REPO_URL", "https://example.com/repo.git"
        )
        monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", "http://localhost:3000")
        clear_config_cache()
        try:
            response = await async_client.post("/api/automation/v1/git-sync/sync")
        finally:
            clear_config_cache()

        assert response.status_code == 202
        assert response.json() == {"triggered": True}

    async def test_background_task_is_not_garbage_collected_mid_run(
        self, async_client, monkeypatch
    ):
        """The triggered sync task must be strongly referenced, or asyncio
        may garbage-collect it before the git I/O completes. Uses a slow
        fake run_sync_cycle so the in-flight assertion below isn't racing
        a real (fast-failing) network call.
        """
        import openhands.automation.git_sync.router as router_module

        async def slow_run_sync_cycle(*args, **kwargs):
            await asyncio.sleep(0.3)

        monkeypatch.setattr(router_module, "run_sync_cycle", slow_run_sync_cycle)

        monkeypatch.setenv("AUTOMATION_GIT_SYNC_ENABLED", "1")
        monkeypatch.setenv(
            "AUTOMATION_GIT_SYNC_REPO_URL", "https://example.com/repo.git"
        )
        monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", "http://localhost:3000")
        clear_config_cache()
        try:
            assert len(_background_sync_tasks) == 0
            response = await async_client.post("/api/automation/v1/git-sync/sync")
            assert response.status_code == 202
            # The task was registered for a strong reference before the
            # handler returned.
            assert len(_background_sync_tasks) == 1
            # It also cleans itself up once done, rather than leaking.
            for _ in range(50):
                if len(_background_sync_tasks) == 0:
                    break
                await asyncio.sleep(0.05)
            assert len(_background_sync_tasks) == 0
        finally:
            clear_config_cache()


class TestSyncInProgressReporting:
    """`GET /status` has to say a cycle is running: it only reports the outcome
    once the cycle ends, so a caller watching `last_synced_at` cannot tell a
    sync in flight from one that never started.
    """

    @pytest.fixture
    async def running_cycle(self, monkeypatch):
        """Hold a cycle open for the duration of a test."""
        import openhands.automation.git_sync.loop as loop_module

        started = asyncio.Event()
        finish = asyncio.Event()

        async def paused_cycle(*args, **kwargs):
            started.set()
            await finish.wait()

        monkeypatch.setattr(loop_module, "_run_sync_cycle_locked", paused_cycle)
        # The cycle body is replaced above, so these are only here to satisfy
        # the signature -- nothing reads them.
        cycle = asyncio.create_task(
            loop_module.run_sync_cycle(
                async_sessionmaker(class_=AsyncSession),
                GitSyncSettings(),
                ServiceSettings(agent_server_url="http://localhost:3000"),
            )
        )
        yield started, finish, cycle
        finish.set()
        await cycle

    async def test_reports_no_sync_when_idle(self, async_client):
        body = (await async_client.get("/api/automation/v1/git-sync/status")).json()

        assert body["sync_in_progress"] is False
        assert body["sync_started_at"] is None

    async def test_reports_a_cycle_started_by_the_periodic_loop(
        self, async_client, running_cycle
    ):
        started, _, _ = running_cycle
        await started.wait()

        body = (await async_client.get("/api/automation/v1/git-sync/status")).json()

        assert body["sync_in_progress"] is True
        assert body["sync_started_at"] is not None

    async def test_trigger_is_a_no_op_while_a_cycle_is_running(
        self, async_client, monkeypatch, running_cycle
    ):
        # `run_sync_cycle` serializes on a lock, so a second cycle would only
        # queue behind the running one and repeat what it already covers.
        started, _, _ = running_cycle
        await started.wait()
        monkeypatch.setenv("AUTOMATION_GIT_SYNC_ENABLED", "1")
        monkeypatch.setenv(
            "AUTOMATION_GIT_SYNC_REPO_URL", "https://example.com/repo.git"
        )
        monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", "http://localhost:3000")
        clear_config_cache()
        try:
            response = await async_client.post("/api/automation/v1/git-sync/sync")
        finally:
            clear_config_cache()

        assert response.status_code == 202
        assert response.json() == {"triggered": False}
        assert len(_background_sync_tasks) == 0


class TestGitSyncConfigCannotEnableSync:
    """Regression: `POST /sync` gated on the override-merged `enabled`, so
    `PUT /config` could newly enable sync in a deployment that booted with
    AUTOMATION_GIT_SYNC_ENABLED unset -- contradicting its own docstring and
    AGENTS.md, and letting any holder of `manage_automations` push every
    automation's prompt, model config and tarball to a repo of their choice.
    """

    async def test_enabling_is_rejected_when_not_opted_in_at_boot(self, async_client):
        response = await async_client.put(
            "/api/automation/v1/git-sync/config",
            json={"enabled": True, "repo_url": "https://example.com/evil.git"},
        )
        assert response.status_code == 409

    async def test_manual_sync_stays_unavailable_after_such_an_attempt(
        self, async_client
    ):
        await async_client.put(
            "/api/automation/v1/git-sync/config",
            json={"repo_url": "https://example.com/evil.git"},
        )
        response = await async_client.post("/api/automation/v1/git-sync/sync")
        assert response.status_code == 503

    async def test_pausing_is_always_allowed(self, async_client):
        response = await async_client.put(
            "/api/automation/v1/git-sync/config", json={"enabled": False}
        )
        assert response.status_code == 200


class TestGitSyncConfigValidation:
    """Regression: `path` was an unvalidated `str`, so a traversing value
    reached `sync_root` -- which the export rmtree's per automation -- and
    the UI's `""` for a cleared field was stored as a literal empty override,
    making `git add -A -- ""` fatal on every subsequent cycle.
    """

    @pytest.mark.parametrize(
        "bad_path", ["../../../../etc", "a/../../b", "..", "  ../x  "]
    )
    async def test_traversing_paths_are_rejected(self, async_client, bad_path):
        response = await async_client.put(
            "/api/automation/v1/git-sync/config", json={"path": bad_path}
        )
        assert response.status_code == 422

    async def test_absolute_path_is_read_as_repo_relative(self, async_client):
        response = await async_client.put(
            "/api/automation/v1/git-sync/config", json={"path": "/automations"}
        )
        assert response.status_code == 200
        assert response.json()["path"] == "automations"

    async def test_trailing_slash_is_stripped(self, async_client):
        """A trailing slash silently muted every import: `_changed_slugs_since`
        matches on an f"{sync_path}/" prefix, so "automations/" produced
        "automations//" and matched nothing."""
        response = await async_client.put(
            "/api/automation/v1/git-sync/config", json={"path": "automations/"}
        )
        assert response.status_code == 200
        assert response.json()["path"] == "automations"

    async def test_blank_path_clears_the_override(self, async_client):
        await async_client.put(
            "/api/automation/v1/git-sync/config", json={"path": "custom"}
        )
        response = await async_client.put(
            "/api/automation/v1/git-sync/config", json={"path": "   "}
        )
        assert response.status_code == 200
        assert response.json()["path"] == "automations"

    async def test_blank_branch_clears_the_override(self, async_client):
        await async_client.put(
            "/api/automation/v1/git-sync/config", json={"branch": "develop"}
        )
        response = await async_client.put(
            "/api/automation/v1/git-sync/config", json={"branch": ""}
        )
        assert response.status_code == 200
        assert response.json()["branch"] == "main"


class TestGitSyncSecretsAtRest:
    async def test_secrets_are_not_stored_in_cleartext(
        self, async_client, async_session
    ):
        """Regression: the token and the encryption key were persisted as
        cleartext JSON in `automation_service_metadata`, readable in any DB
        dump or backup."""
        from openhands.automation.git_sync.config_override import (
            GIT_SYNC_CONFIG_OVERRIDE_KEY,
        )
        from openhands.automation.utils.service_metadata import get_service_metadata

        response = await async_client.put(
            "/api/automation/v1/git-sync/config",
            json={"token": "ghp_supersecrettoken", "encryption_key": "the-key"},
        )
        assert response.status_code == 200

        raw = await get_service_metadata(async_session, GIT_SYNC_CONFIG_OVERRIDE_KEY)
        assert raw is not None
        assert "ghp_supersecrettoken" not in raw
        assert "the-key" not in raw

    async def test_stored_secrets_are_readable_again(self, async_client, async_session):
        from openhands.automation.config import get_config
        from openhands.automation.git_sync.config_override import (
            resolve_effective_git_sync_settings,
        )

        await async_client.put(
            "/api/automation/v1/git-sync/config",
            json={"token": "ghp_supersecrettoken"},
        )
        effective = await resolve_effective_git_sync_settings(
            async_session, get_config().git_sync
        )
        assert effective.git_sync_token == "ghp_supersecrettoken"


class TestGitSyncConfigCheck:
    """`POST /check` answers "would this configuration reach its repo?".

    It exists so an operator does not have to save a repo URL and wait for a
    cycle to find out it was a typo -- and so that finding out never means
    running a sync against a repo nobody has vetted yet.
    """

    @pytest.fixture
    def captured_check(self, monkeypatch):
        """Record what the endpoint asks git about, without touching a remote."""
        import openhands.automation.git_sync.router as router_module

        calls: list[tuple] = []

        async def fake_check(repo_url, branch, token, timeout):
            calls.append((repo_url, branch, token, timeout))
            return True

        monkeypatch.setattr(router_module, "check_remote_access", fake_check)
        return calls

    async def test_requires_manage_automations_permission(self, readonly_client):
        response = await readonly_client.post(
            "/api/automation/v1/git-sync/check", json={}
        )
        assert response.status_code == 403

    async def test_reports_a_missing_repo_url_without_running_git(
        self, async_client, captured_check
    ):
        response = await async_client.post("/api/automation/v1/git-sync/check", json={})

        assert response.status_code == 200
        assert response.json()["ok"] is False
        assert response.json()["detail"]
        assert captured_check == []

    async def test_checks_the_submitted_values_not_the_saved_ones(
        self, async_client, captured_check
    ):
        """The whole point: test the form before it is saved."""
        await async_client.put(
            "/api/automation/v1/git-sync/config",
            json={"repo_url": "https://example.com/saved.git", "branch": "saved"},
        )

        response = await async_client.post(
            "/api/automation/v1/git-sync/check",
            json={"repo_url": "https://example.com/candidate.git", "branch": "new"},
        )

        assert response.status_code == 200
        assert captured_check == [
            ("https://example.com/candidate.git", "new", "", 20.0)
        ]

    async def test_saves_nothing(self, async_client, captured_check):
        await async_client.put(
            "/api/automation/v1/git-sync/config",
            json={"repo_url": "https://example.com/saved.git", "branch": "saved"},
        )

        await async_client.post(
            "/api/automation/v1/git-sync/check",
            json={"repo_url": "https://example.com/candidate.git", "branch": "new"},
        )

        body = (await async_client.get("/api/automation/v1/git-sync/status")).json()
        assert body["repo_url"] == "https://example.com/saved.git"
        assert body["branch"] == "saved"

    async def test_omitted_fields_come_from_the_saved_config(
        self, async_client, captured_check
    ):
        await async_client.put(
            "/api/automation/v1/git-sync/config",
            json={
                "repo_url": "https://example.com/saved.git",
                "branch": "saved",
                "token": "saved-token",
            },
        )

        await async_client.post(
            "/api/automation/v1/git-sync/check", json={"branch": "new"}
        )

        assert captured_check == [
            ("https://example.com/saved.git", "new", "saved-token", 20.0)
        ]

    async def test_null_is_checked_as_the_env_default_not_as_blank(
        self, async_client, captured_check, monkeypatch
    ):
        """Clearing a field reverts it to the env var, so that -- not an empty
        string -- is the value the check has to report on."""
        monkeypatch.setenv(
            "AUTOMATION_GIT_SYNC_REPO_URL", "https://example.com/from-env.git"
        )
        clear_config_cache()
        try:
            await async_client.put(
                "/api/automation/v1/git-sync/config",
                json={"repo_url": "https://example.com/override.git"},
            )
            await async_client.post(
                "/api/automation/v1/git-sync/check", json={"repo_url": None}
            )
        finally:
            clear_config_cache()

        assert captured_check[0][0] == "https://example.com/from-env.git"

    async def test_an_unreachable_remote_is_a_200_with_ok_false(
        self, async_client, monkeypatch
    ):
        """A bad configuration is an answer, not a server error -- the caller
        renders `detail`, so it must not have to parse an error response."""
        import openhands.automation.git_sync.client as client_module
        import openhands.automation.git_sync.router as router_module

        async def fail(*args, **kwargs):
            raise client_module.GitSyncError("fatal: repository not found")

        monkeypatch.setattr(router_module, "check_remote_access", fail)

        response = await async_client.post(
            "/api/automation/v1/git-sync/check",
            json={"repo_url": "https://example.com/gone.git"},
        )

        assert response.status_code == 200
        assert response.json()["ok"] is False
        assert "repository not found" in response.json()["detail"]

    async def test_a_branch_that_does_not_exist_yet_still_passes(
        self, async_client, monkeypatch
    ):
        """The first sync creates it, so this must not read as misconfigured."""
        import openhands.automation.git_sync.router as router_module

        async def no_branch(*args, **kwargs):
            return False

        monkeypatch.setattr(router_module, "check_remote_access", no_branch)

        response = await async_client.post(
            "/api/automation/v1/git-sync/check",
            json={"repo_url": "https://example.com/fresh.git"},
        )

        assert response.json() == {"ok": True, "branch_exists": False, "detail": None}

    async def test_works_while_sync_is_disabled(self, async_client, captured_check):
        """Getting the repo URL and token right is what you do *before*
        enabling sync, so this cannot be gated on it being enabled."""
        response = await async_client.post(
            "/api/automation/v1/git-sync/check",
            json={"repo_url": "https://example.com/repo.git"},
        )

        assert response.status_code == 200
        assert response.json()["ok"] is True


class TestGitSyncCheckAgainstARealRepo:
    """One end-to-end pass with no stub between the endpoint and git, so the
    two halves can't drift: the router calling `check_remote_access` with its
    arguments in some other order would still satisfy every mocked test."""

    @pytest.fixture
    def origin(self, tmp_path):
        origin_dir = tmp_path / "origin"
        origin_dir.mkdir()
        subprocess.run(
            ["git", "init", "--bare", "-q", "-b", "main"], cwd=origin_dir, check=True
        )
        return origin_dir

    async def test_reaches_a_real_remote(self, async_client, origin):
        response = await async_client.post(
            "/api/automation/v1/git-sync/check",
            json={"repo_url": f"file://{origin}", "branch": "main"},
        )

        assert response.status_code == 200
        # A branch nothing has pushed to yet: reachable, not yet created.
        assert response.json() == {"ok": True, "branch_exists": False, "detail": None}

    async def test_reports_a_remote_that_is_not_there(self, async_client, tmp_path):
        response = await async_client.post(
            "/api/automation/v1/git-sync/check",
            json={"repo_url": f"file://{tmp_path / 'missing'}", "branch": "main"},
        )

        body = response.json()
        assert body["ok"] is False
        assert "missing" in body["detail"]
