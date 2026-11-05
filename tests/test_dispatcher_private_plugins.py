"""Dispatching a plugin-spec automation whose plugin is in a private repository.

The service preflights the plugin before a sandbox exists, then the agent
server fetches it. Both fetches here run the SDK's real strict fetch against
a git server that only serves its token, resolving ``${PLUGIN_TOKEN}`` through
a real secret store: the agent server's in local mode.
"""

import json
import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from openhands.automation.config import clear_config_cache
from openhands.automation.dispatcher import _execute_run
from openhands.automation.models import (
    Automation,
    AutomationRun,
    AutomationRunStatus,
)
from openhands.automation.plugin_secrets import PluginSourceSecrets
from openhands.automation.plugin_specs import PluginSpecError
from openhands.automation.utils import utcnow
from openhands.sdk.plugin.strict_fetch import PluginSourceError, fetch_plugin_strict
from openhands.sdk.secret import LookupSecret, SecretSource
from tests.private_git_server import PrivateGitServer, SecretStoreServer, make_bare_repo


TOKEN = "fake-git-token-abcdef0123"
SESSION_KEY = "fake-agent-session-key"
TEST_USER_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
TEST_ORG_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")
MANIFEST = {
    "name": "private-plugin",
    "version": "0.1.0",
    "automation": {
        "schema_version": "openhands.automation.plugin.v1",
        "entrypoint": {"runner": "script", "command": "bash run.sh"},
    },
}


class FakeAgentServer:
    """``POST /api/plugins/fetch`` as the agent server implements it."""

    def __init__(self, cache_dir) -> None:
        self.cache_dir = cache_dir
        self.requests: list[dict] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/plugins/fetch"
        assert request.headers["X-Session-API-Key"] == SESSION_KEY
        body = json.loads(request.content)
        self.requests.append(body)
        secrets = {
            name: SecretSource.model_validate(value)
            for name, value in body.get("secrets", {}).items()
        }
        try:
            checked = fetch_plugin_strict(
                source=body["source"],
                ref=body.get("ref"),
                repo_path=body.get("repo_path"),
                secrets=secrets,
                cache_dir=self.cache_dir,
                allow_local=True,
            )
        except PluginSourceError as exc:
            return httpx.Response(
                exc.status_code,
                json={"detail": {"code": exc.code, "message": exc.message}},
            )
        return httpx.Response(
            200,
            json={
                "path": str(checked.path),
                "resolved_ref": checked.resolved_ref,
                "source": checked.source,
                "repo_path": checked.repo_path,
                "manifest": checked.manifest,
                "skills": checked.skills,
                "commands": checked.commands,
            },
        )


@pytest.fixture
def private_plugin(tmp_path):
    sha = make_bare_repo(
        tmp_path,
        "plugin",
        {".plugin/plugin.json": json.dumps(MANIFEST), "run.sh": "echo ran\n"},
    )
    with PrivateGitServer(tmp_path, TOKEN) as server:
        host = server.url.removeprefix("http://")
        yield f"http://x-access-token:${{PLUGIN_TOKEN}}@{host}/plugin.git", sha


@pytest.fixture
def local_agent_server(monkeypatch, tmp_path):
    """Local mode: the agent server's secret store and plugin fetch."""
    with SecretStoreServer(SESSION_KEY, {"PLUGIN_TOKEN": TOKEN}) as store:
        monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", store.url)
        monkeypatch.setenv("AUTOMATION_AGENT_SERVER_API_KEY", SESSION_KEY)
        monkeypatch.setenv("AUTOMATION_WORKSPACE_BASE", str(tmp_path / "workspaces"))
        clear_config_cache()
        agent = FakeAgentServer(tmp_path / "plugin-cache")
        yield store, agent
    clear_config_cache()


async def _private_plugin_run(async_session_factory, source: str):
    async with async_session_factory() as session:
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Private plugin",
            trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
            tarball_path="plugin-spec://runtime",
            entrypoint='cd "$AUTOMATION_PLUGIN_ROOT" && bash run.sh',
            plugin_spec={"plugin": {"source": source}, "config": {}},
            enabled=True,
        )
        session.add(automation)
        await session.commit()
        run = AutomationRun(
            automation_id=automation.id,
            status=AutomationRunStatus.RUNNING,
            started_at=utcnow(),
        )
        session.add(run)
        await session.commit()
        run_id, automation_id = run.id, automation.id
    async with async_session_factory() as session:
        run = (
            await session.execute(
                select(AutomationRun)
                .options(selectinload(AutomationRun.automation))
                .where(AutomationRun.id == run_id)
            )
        ).scalar_one()
    return run, run_id, automation_id


async def _dispatch(run, settings, session_factory, agent: FakeAgentServer):
    started = SimpleNamespace(
        success=True, sandbox_id=None, error=None, bash_command_id="cmd-1"
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(agent.handle)) as client:
        with patch(
            "openhands.automation.dispatcher.execute_in_context",
            new=AsyncMock(return_value=started),
        ) as execute:
            await _execute_run(run, settings, session_factory, client)
    return execute


async def test_local_run_fetches_a_private_plugin_at_both_points(
    async_session_factory, mock_settings, private_plugin, local_agent_server, caplog
):
    caplog.set_level(logging.DEBUG)
    source, sha = private_plugin
    store, agent = local_agent_server
    run, run_id, automation_id = await _private_plugin_run(
        async_session_factory, source
    )

    execute = await _dispatch(run, mock_settings, async_session_factory, agent)

    execute.assert_awaited_once()
    # The service's preflight and the agent server's fetch each read the
    # secret from the agent server's store.
    assert store.requests == ["/api/settings/secrets/PLUGIN_TOKEN"] * 2
    # The agent server got the source unexpanded plus a reference it resolves.
    [fetch] = agent.requests
    assert fetch["source"] == source
    assert fetch["secrets"] == {
        "PLUGIN_TOKEN": {
            "kind": "LookupSecret",
            "description": None,
            "url": f"{store.url}/api/settings/secrets/PLUGIN_TOKEN",
            "headers": {"X-Session-API-Key": SESSION_KEY},
        }
    }
    async with async_session_factory() as session:
        updated = await session.get(AutomationRun, run_id)
        automation = await session.get(Automation, automation_id)
    assert updated.status == AutomationRunStatus.RUNNING
    assert updated.resolved_runtime["plugin_resolved_ref"] == sha
    assert updated.resolved_runtime["fetched_plugins"][0]["source"] == source
    assert automation.plugin_spec["plugin"]["source"] == source
    stored = json.dumps(
        [updated.resolved_runtime, updated.status_detail, automation.plugin_spec]
    )
    assert TOKEN not in stored
    assert TOKEN not in caplog.text
    # The entrypoint gets the plugin root, never the credential.
    assert execute.await_args is not None
    env = execute.await_args.kwargs["env_vars"]
    assert env["AUTOMATION_PLUGIN_ROOT"] == updated.resolved_runtime["plugin_path"]
    assert TOKEN not in json.dumps(env)


async def test_local_run_with_a_deleted_secret_fails_permanently(
    async_session_factory, mock_settings, private_plugin, local_agent_server
):
    source, _sha = private_plugin
    store, agent = local_agent_server
    store.secrets.clear()
    run, run_id, automation_id = await _private_plugin_run(
        async_session_factory, source
    )

    execute = await _dispatch(run, mock_settings, async_session_factory, agent)

    execute.assert_not_awaited()
    assert agent.requests == []
    async with async_session_factory() as session:
        updated = await session.get(AutomationRun, run_id)
        automation = await session.get(Automation, automation_id)
    assert updated.status == AutomationRunStatus.FAILED
    assert "Missing secret: PLUGIN_TOKEN" in updated.error_detail
    assert updated.status_detail["code"] == "missing_secret"
    assert updated.status_detail["operation"] == "preflight_plugin"
    assert updated.status_detail["transient"] is False
    assert automation.enabled is False


async def test_local_run_with_a_rejected_secret_fails_permanently(
    async_session_factory, mock_settings, private_plugin, local_agent_server
):
    source, _sha = private_plugin
    store, agent = local_agent_server
    store.secrets["PLUGIN_TOKEN"] = "fake-revoked-token"
    run, run_id, automation_id = await _private_plugin_run(
        async_session_factory, source
    )

    await _dispatch(run, mock_settings, async_session_factory, agent)

    async with async_session_factory() as session:
        updated = await session.get(AutomationRun, run_id)
        automation = await session.get(Automation, automation_id)
    assert updated.status == AutomationRunStatus.FAILED
    assert updated.status_detail["code"] == "auth_failed"
    assert "PLUGIN_TOKEN" in updated.error_detail
    assert "fake-revoked-token" not in updated.error_detail
    assert automation.enabled is False


# ---------------------------------------------------------------------------
# The same steps with the references a cloud backend returns
# ---------------------------------------------------------------------------


async def _cloud_dispatch(
    run, settings, session_factory, agent, secrets, *, execute_result=None
):
    """Dispatch with a backend whose references are ``secrets``.

    The backend allows local sources only because the test git server speaks
    plain HTTP; the references have the shape a cloud backend returns.
    """
    backend = MagicMock()
    backend.is_local_mode = True
    backend.get_work_dir = MagicMock(return_value="/workspace/project")
    backend.plugin_source_secrets = secrets
    ctx = SimpleNamespace(
        agent_url="http://sandbox-agent",
        sandbox_id="sbx-1",
        session_key=SESSION_KEY,
        api_key="fake-run-api-key",
        api_url="https://app.test",
    )
    backend.get_execution_context = AsyncMock(return_value=ctx)
    backend.release_context = AsyncMock()
    backend.build_env_vars = MagicMock(return_value={})
    started = execute_result or SimpleNamespace(
        success=True, sandbox_id="sbx-1", error=None, bash_command_id="cmd-1"
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(agent.handle)) as client:
        with (
            patch("openhands.automation.dispatcher.get_backend", return_value=backend),
            patch(
                "openhands.automation.dispatcher.execute_in_context",
                new=AsyncMock(return_value=started),
            ),
        ):
            await _execute_run(run, settings, session_factory, client)
    return backend


@pytest.fixture
def lookup_endpoint():
    """A stand-in for the app server's ``/api/v1/webhooks/secrets``."""
    with SecretStoreServer("fake-jws", {"PLUGIN_TOKEN": TOKEN}) as store:
        yield store


def _cloud_references(store: SecretStoreServer) -> PluginSourceSecrets:
    reference = LookupSecret(
        url=f"{store.url}/api/settings/secrets/PLUGIN_TOKEN",
        headers={"X-Session-API-Key": "fake-jws"},
    )
    return PluginSourceSecrets(
        preflight={"PLUGIN_TOKEN": reference}, fetch={"PLUGIN_TOKEN": reference}
    )


async def test_cloud_run_asks_for_fresh_references_at_each_fetch(
    async_session_factory, mock_settings, private_plugin, lookup_endpoint, tmp_path
):
    source, sha = private_plugin
    agent = FakeAgentServer(tmp_path / "cache")
    run, run_id, _automation_id = await _private_plugin_run(
        async_session_factory, source
    )
    secrets = AsyncMock(return_value=_cloud_references(lookup_endpoint))

    await _cloud_dispatch(run, mock_settings, async_session_factory, agent, secrets)

    assert [call.args[1] for call in secrets.await_args_list] == [
        ["PLUGIN_TOKEN"],
        ["PLUGIN_TOKEN"],
    ]
    assert agent.requests[0]["secrets"]["PLUGIN_TOKEN"]["headers"] == {
        "X-Session-API-Key": "fake-jws"
    }
    async with async_session_factory() as session:
        updated = await session.get(AutomationRun, run_id)
    assert updated.status == AutomationRunStatus.RUNNING
    assert updated.resolved_runtime["plugin_resolved_ref"] == sha
    assert "fake-jws" not in json.dumps(updated.resolved_runtime)


@pytest.mark.parametrize(
    ("error", "transient", "enabled"),
    [
        (
            PluginSpecError(
                "Private plugin sources are not supported on this deployment yet",
                code="private_source_unsupported",
                status_code=422,
            ),
            False,
            False,
        ),
        (
            PluginSpecError(
                "Missing secret: PLUGIN_TOKEN", code="missing_secret", status_code=422
            ),
            False,
            False,
        ),
        (
            PluginSpecError(
                "The OpenHands API could not resolve secret(s)",
                code="secret_unavailable",
                status_code=503,
            ),
            True,
            True,
        ),
    ],
)
async def test_cloud_preflight_lookup_failures_are_classified(
    async_session_factory,
    mock_settings,
    private_plugin,
    tmp_path,
    error,
    transient,
    enabled,
):
    source, _sha = private_plugin
    agent = FakeAgentServer(tmp_path / "cache")
    run, run_id, automation_id = await _private_plugin_run(
        async_session_factory, source
    )

    backend = await _cloud_dispatch(
        run,
        mock_settings,
        async_session_factory,
        agent,
        AsyncMock(side_effect=error),
    )

    backend.get_execution_context.assert_not_called()
    async with async_session_factory() as session:
        updated = await session.get(AutomationRun, run_id)
        automation = await session.get(Automation, automation_id)
    assert updated.status == AutomationRunStatus.FAILED
    assert updated.status_detail["operation"] == "preflight_plugin"
    assert updated.status_detail["code"] == error.code
    assert updated.status_detail["transient"] is transient
    assert automation.enabled is enabled


@pytest.mark.parametrize(
    ("fetch_error", "transient", "enabled"),
    [
        (
            PluginSpecError(
                "Missing secret: PLUGIN_TOKEN", code="missing_secret", status_code=422
            ),
            False,
            False,
        ),
        (
            PluginSpecError(
                "The OpenHands API could not resolve secret(s)",
                code="secret_unavailable",
                status_code=503,
            ),
            True,
            True,
        ),
    ],
)
async def test_cloud_fetch_lookup_failures_release_the_sandbox(
    async_session_factory,
    mock_settings,
    private_plugin,
    lookup_endpoint,
    tmp_path,
    fetch_error,
    transient,
    enabled,
):
    """The references for the agent server are requested after the sandbox
    starts; a failure there releases the sandbox and is classified too."""
    source, _sha = private_plugin
    agent = FakeAgentServer(tmp_path / "cache")
    run, run_id, automation_id = await _private_plugin_run(
        async_session_factory, source
    )
    secrets = AsyncMock(side_effect=[_cloud_references(lookup_endpoint), fetch_error])

    backend = await _cloud_dispatch(
        run, mock_settings, async_session_factory, agent, secrets
    )

    backend.release_context.assert_awaited_once()
    assert agent.requests == []
    async with async_session_factory() as session:
        updated = await session.get(AutomationRun, run_id)
        automation = await session.get(Automation, automation_id)
    assert updated.status == AutomationRunStatus.FAILED
    assert updated.status_detail["operation"] == "fetch_plugin"
    assert updated.status_detail["code"] == fetch_error.code
    assert updated.status_detail["transient"] is transient
    assert automation.enabled is enabled


async def test_agent_server_fetch_errors_keep_their_code(
    async_session_factory, mock_settings, private_plugin, lookup_endpoint, tmp_path
):
    """The secret disappears between the preflight and the sandbox fetch."""
    source, _sha = private_plugin
    agent = FakeAgentServer(tmp_path / "cache")
    run, run_id, automation_id = await _private_plugin_run(
        async_session_factory, source
    )
    gone = LookupSecret(url=f"{lookup_endpoint.url}/api/settings/secrets/GONE")
    secrets = AsyncMock(
        side_effect=[
            _cloud_references(lookup_endpoint),
            PluginSourceSecrets(fetch={"PLUGIN_TOKEN": gone}),
        ]
    )

    def missing_on_agent(request: httpx.Request) -> httpx.Response:
        agent.requests.append(json.loads(request.content))
        return httpx.Response(
            422,
            json={"detail": {"code": "missing_secret", "message": "Missing secret"}},
        )

    agent.handle = missing_on_agent  # type: ignore[method-assign]
    await _cloud_dispatch(run, mock_settings, async_session_factory, agent, secrets)

    async with async_session_factory() as session:
        updated = await session.get(AutomationRun, run_id)
        automation = await session.get(Automation, automation_id)
    assert updated.status == AutomationRunStatus.FAILED
    assert updated.status_detail["code"] == "missing_secret"
    assert updated.status_detail["status_code"] == 422
    assert updated.status_detail["transient"] is False
    assert automation.enabled is False
