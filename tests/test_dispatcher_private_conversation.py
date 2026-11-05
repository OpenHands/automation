"""A plugin-spec conversation run whose plugin is in a private repository.

The service preflights the plugin, then the agent server in the run's sandbox
fetches it with references for the secrets its source names (#696). The app
server then starts the run's conversation in that sandbox with the same plugin
spec the stock runner would have used: the source keeps its ``${NAME}``
placeholder, and neither a secret value nor a reference reaches the start
request. The agent server resolves the placeholder from the conversation's
secrets when it loads the plugin, as it does when the runner creates the
conversation, and finds the checkout the fetch pinned.
"""

import dataclasses
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from openhands.automation.backends.base import StartedConversation
from openhands.automation.dispatcher import _execute_run
from openhands.automation.models import Automation, AutomationRun, AutomationRunStatus
from openhands.automation.plugin_specs import PluginSpecError
from openhands.automation.utils import utcnow
from tests.private_git_server import PrivateGitServer, make_bare_repo
from tests.test_dispatcher_private_plugins import (
    SESSION_KEY,
    TEST_ORG_ID,
    TEST_USER_ID,
    TOKEN,
    FakeAgentServer,
    _cloud_references,
    lookup_endpoint,  # noqa: F401  (fixture)
)


PROFILE_ID = "0a1b2c3d-0000-4000-8000-000000000002"
CONVERSATION_ID = "9f1b2c3d-0000-4000-8000-00000000dddd"
PLUGIN = {
    "name": "private-conversation-plugin",
    "version": "0.1.0",
    "automation": {
        "schema_version": "openhands.automation.plugin.v1",
        "entrypoint": {"runner": "conversation", "command": "hello"},
        "runtime": {"agent_profile": {"required": True, "default_name": "agent"}},
    },
}


@pytest.fixture
def private_conversation_plugin(tmp_path):
    sha = make_bare_repo(
        tmp_path,
        "plugin",
        {
            ".plugin/plugin.json": json.dumps(PLUGIN),
            "commands/hello.md": "Say hello.\n",
        },
    )
    with PrivateGitServer(tmp_path, TOKEN) as server:
        host = server.url.removeprefix("http://")
        yield f"http://x-access-token:${{PLUGIN_TOKEN}}@{host}/plugin.git", sha


async def _conversation_run(async_session_factory, source: str):
    async with async_session_factory() as session:
        automation = Automation(
            user_id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            name="Private conversation plugin",
            trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
            tarball_path="plugin-spec://runtime",
            entrypoint="python main.py",
            plugin_spec={"plugin": {"source": source}, "config": {}},
            agent_profile_id=uuid.UUID(PROFILE_ID),
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


async def _dispatch(run, settings, session_factory, agent, secrets):
    """Dispatch with a backend that starts conversations through the app server.

    As in #696's tests, the backend allows local sources only because the test
    git server speaks plain HTTP; the references have a cloud backend's shape.
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
    fetches_before_start: list[int] = []

    async def start(_client, _ctx, _start):
        fetches_before_start.append(len(agent.requests))
        return StartedConversation(
            conversation_id=CONVERSATION_ID, agent_profile_id=PROFILE_ID
        )

    backend.start_conversation = AsyncMock(side_effect=start)
    executed = SimpleNamespace(
        success=True, sandbox_id="sbx-1", error=None, bash_command_id="cmd-1"
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(agent.handle)) as client:
        with (
            patch("openhands.automation.dispatcher.get_backend", return_value=backend),
            # The profile check reads the app server; profile validation has
            # its own tests.
            patch(
                "openhands.automation.dispatcher._validate_plugin_run_agent_profile",
                new=AsyncMock(return_value=(PROFILE_ID, "openhands")),
            ),
            patch(
                "openhands.automation.dispatcher.execute_in_context",
                new=AsyncMock(return_value=executed),
            ) as execute,
        ):
            await _execute_run(run, settings, session_factory, client)
    return backend, execute, fetches_before_start


async def test_the_app_server_gets_the_placeholder_source_after_the_fetch(
    async_session_factory,
    mock_settings,
    private_conversation_plugin,
    lookup_endpoint,  # noqa: F811
    tmp_path,
):
    source, sha = private_conversation_plugin
    agent = FakeAgentServer(tmp_path / "cache")
    run, run_id, _ = await _conversation_run(async_session_factory, source)
    secrets = AsyncMock(return_value=_cloud_references(lookup_endpoint))

    backend, execute, fetches_before_start = await _dispatch(
        run, mock_settings, async_session_factory, agent, secrets
    )

    # #696 unchanged: fresh references at the preflight and at the fetch, and
    # the agent server fetched the source unexpanded, with those references.
    assert [call.args[1] for call in secrets.await_args_list] == [
        ["PLUGIN_TOKEN"],
        ["PLUGIN_TOKEN"],
    ]
    assert [request["source"] for request in agent.requests] == [source]
    assert agent.requests[0]["secrets"]["PLUGIN_TOKEN"]["headers"] == {
        "X-Session-API-Key": "fake-jws"
    }
    # The conversation starts only after the plugin is in the sandbox.
    assert fetches_before_start == [1]
    start = backend.start_conversation.await_args.args[2]
    assert start.agent_profile_id == PROFILE_ID
    assert start.plugins == [{"source": source, "ref": sha}]
    assert "${PLUGIN_TOKEN}" in start.plugins[0]["source"]
    sent = json.dumps(dataclasses.asdict(start))
    assert TOKEN not in sent
    assert "fake-jws" not in sent
    assert lookup_endpoint.url not in sent

    executed = execute.await_args
    assert executed is not None
    env_vars = executed.kwargs["env_vars"]
    assert env_vars["AUTOMATION_CONVERSATION_STARTED"] == "app_server"
    assert env_vars["AUTOMATION_CONVERSATION_ID"] == CONVERSATION_ID
    assert env_vars["AUTOMATION_PLUGIN_ROOT"].startswith(str(tmp_path / "cache"))
    assert not any(TOKEN in value or "fake-jws" in value for value in env_vars.values())
    async with async_session_factory() as session:
        updated = await session.get(AutomationRun, run_id)
    assert updated.status == AutomationRunStatus.RUNNING
    assert updated.resolved_runtime["plugin_resolved_ref"] == sha
    stored = json.dumps(updated.resolved_runtime)
    assert TOKEN not in stored
    assert "fake-jws" not in stored


async def test_a_missing_secret_at_the_fetch_fails_before_the_start(
    async_session_factory,
    mock_settings,
    private_conversation_plugin,
    lookup_endpoint,  # noqa: F811
    tmp_path,
):
    """#696's failure classes hold: nothing starts and nothing runs."""
    source, _sha = private_conversation_plugin
    agent = FakeAgentServer(tmp_path / "cache")
    run, run_id, automation_id = await _conversation_run(async_session_factory, source)
    secrets = AsyncMock(
        side_effect=[
            _cloud_references(lookup_endpoint),
            PluginSpecError(
                "Missing secret: PLUGIN_TOKEN", code="missing_secret", status_code=422
            ),
        ]
    )

    backend, execute, _ = await _dispatch(
        run, mock_settings, async_session_factory, agent, secrets
    )

    backend.start_conversation.assert_not_awaited()
    execute.assert_not_awaited()
    backend.release_context.assert_awaited_once()
    async with async_session_factory() as session:
        updated = await session.get(AutomationRun, run_id)
        automation = await session.get(Automation, automation_id)
    assert updated.status == AutomationRunStatus.FAILED
    assert updated.status_detail["code"] == "missing_secret"
    assert updated.status_detail["transient"] is False
    assert automation.enabled is False
