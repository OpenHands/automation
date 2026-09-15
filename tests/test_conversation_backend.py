"""Conversation execution across Agent Server workspace runtimes."""

import json
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest

from openhands.automation.backends.conversation import ConversationBackend
from openhands.automation.backends.local import LocalAgentServerBackend
from openhands.automation.execution import execute_in_context
from openhands.automation.models import Automation, AutomationRun
from openhands.automation.subjects import conversation_id_for
from openhands.sdk import LLM, Agent
from openhands.sdk.conversation.request import StartConversationRequest


def _run(subject: str | None = None) -> AutomationRun:
    automation = Automation(
        id=uuid4(),
        org_id=uuid4(),
        name="portable workflow",
        trigger={"type": "event", "source": "github-events"},
    )
    return AutomationRun(
        id=uuid4(),
        automation=automation,
        agent_profile_id=uuid4(),
        execution_scope="conversation",
        subject_key=subject,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", ["local", "docker"])
@pytest.mark.parametrize("subject", [None, "org/repo/42"])
async def test_same_bundle_contract_and_scoped_execution(
    runtime, subject, tmp_path, sdk_http_transport, monkeypatch
):
    run = _run(subject)
    conversation_id = (
        conversation_id_for(
            run.automation.org_id,
            run.automation.id,
            "github-events",
            subject,
        )
        if subject
        else str(run.id)
    )
    server = LocalAgentServerBackend(
        "http://server",
        "host-key",
        run,
        workspace_base=str(tmp_path),
        callback_api_key="callback-key",
    )
    backend = ConversationBackend(server, run)
    requests = []
    monkeypatch.setattr(
        "openhands.sdk.conversation.impl.remote_conversation.WebSocketCallbackClient",
        MagicMock(),
    )
    agent = Agent(llm=LLM(model="test-model", api_key="test-key"))

    def respond(request):
        requests.append(request)
        if request.url.path == "/server_info":
            return httpx.Response(200, json={"conversation_runtime": runtime})
        if request.url.path == "/api/conversations":
            StartConversationRequest.model_validate_json(request.content)
            return httpx.Response(
                200,
                json={
                    "id": conversation_id,
                    "agent": agent.model_dump(mode="json"),
                    "max_iterations": 160,
                },
            )
        return httpx.Response(
            200, json={"id": "command", "session_api_key": "inner-key", "items": []}
        )

    sdk_http_transport(respond)
    async with httpx.AsyncClient() as client:
        context = await backend.get_execution_context(client)
        env = backend.build_env_vars()
        assert set(env) == {
            "AGENT_SERVER_URL",
            "AUTOMATION_AGENT_PROFILE_ID",
            "AUTOMATION_CONVERSATION_ID",
            "SESSION_API_KEY",
            "WORKSPACE_BASE",
        }
        assert env["AUTOMATION_CONVERSATION_ID"] == conversation_id
        assert env["AUTOMATION_AGENT_PROFILE_ID"] == str(run.agent_profile_id)
        assert env["SESSION_API_KEY"] == (
            "inner-key" if runtime == "docker" else "host-key"
        )
        if runtime == "docker":
            assert "host-key" not in str(env)

        creation = next(r for r in requests if r.url.path == "/api/conversations")
        payload = json.loads(creation.content)
        assert payload["workspace"]["working_dir"] == env["WORKSPACE_BASE"]
        assert payload["conversation_id"] == conversation_id
        assert payload["agent_profile_id"] == str(run.agent_profile_id)

        result = await execute_in_context(
            context.agent_url,
            context.session_key,
            "python3 main.py",
            b"same-bundle",
            env["WORKSPACE_BASE"],
            env,
            run_id=str(run.id),
            runtime_conversation_id=context.runtime_conversation_id,
        )
        assert result.success
        execution = [
            r for r in requests if "/file/" in r.url.path or "/bash/" in r.url.path
        ]
        assert execution
        assert all(
            r.url.path.startswith(f"/api/conversations/{conversation_id}/")
            for r in execution
        )

        before = len(requests)
        await backend.release_context(client, context)
        if runtime == "local":
            assert len(requests) == before
        else:
            assert requests[-1].method == "DELETE"


@pytest.mark.parametrize(
    ("execution_scope", "expected_type"),
    [("run", LocalAgentServerBackend), ("conversation", ConversationBackend)],
)
def test_execution_scope_selects_backend(monkeypatch, execution_scope, expected_type):
    from openhands.automation.backends import get_backend
    from openhands.automation.config import clear_config_cache

    monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", "http://server")
    clear_config_cache()
    try:
        run = _run()
        run.execution_scope = execution_scope
        assert type(get_backend(run)) is expected_type
    finally:
        clear_config_cache()


def test_conversation_execution_requires_profile(monkeypatch):
    from openhands.automation.backends import get_backend
    from openhands.automation.config import clear_config_cache

    monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", "http://server")
    clear_config_cache()
    try:
        run = _run()
        run.agent_profile_id = None
        with pytest.raises(ValueError, match="requires an agent profile"):
            get_backend(run)
    finally:
        clear_config_cache()
