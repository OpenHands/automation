"""Tests for the git sync status/trigger API endpoints."""

import asyncio

from openhands.automation.config import clear_config_cache
from openhands.automation.git_sync.router import _background_sync_tasks


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
