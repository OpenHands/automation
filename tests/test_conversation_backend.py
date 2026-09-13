"""Identical bundle-facing contract across advertised workspace runtimes."""

import json
from uuid import uuid4

import httpx
import pytest

from openhands.automation.backends.conversation import ConversationAgentServerBackend
from openhands.automation.execution import execute_in_context
from openhands.automation.models import Automation, AutomationRun
from openhands.automation.subjects import conversation_id_for


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", ["local", "docker"])
@pytest.mark.parametrize("subject", [None, "org/repo/42"])
async def test_same_bundle_contract_and_scoped_execution(runtime, subject, tmp_path):
    automation = Automation(
        id=uuid4(),
        org_id=uuid4(),
        name="portable workflow",
        trigger={"type": "event", "source": "github-events"},
    )
    run = AutomationRun(id=uuid4(), automation=automation, subject_key=subject)
    conversation_id = (
        conversation_id_for(automation.org_id, automation.id, "github-events", subject)
        if subject
        else str(run.id)
    )
    backend = ConversationAgentServerBackend(
        "http://server",
        "host-key",
        run,
        workspace_base=str(tmp_path),
        callback_api_key="callback-key",
    )
    backend.agent_profile_id = str(uuid4())
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path == "/server_info":
            return httpx.Response(200, json={"conversation_runtime": runtime})
        return httpx.Response(
            200, json={"id": "command", "session_api_key": "inner-key"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        context = await backend.get_execution_context(client)
        env = backend.build_env_vars()
        assert set(env) == {
            "AGENT_SERVER_URL",
            "SESSION_API_KEY",
            "AUTOMATION_CONVERSATION_ID",
            "AUTOMATION_AGENT_PROFILE_ID",
            "WORKSPACE_BASE",
        }
        assert env["AUTOMATION_CONVERSATION_ID"] == conversation_id
        assert env["WORKSPACE_BASE"] == backend.get_work_dir(str(run.id))
        assert env["SESSION_API_KEY"] == (
            "inner-key" if runtime == "docker" else "host-key"
        )
        if runtime == "docker":
            assert "host-key" not in str(env)
        creation = next(r for r in requests if r.url.path == "/api/conversations")
        payload = json.loads(creation.content)
        assert payload["workspace"]["working_dir"] == env["WORKSPACE_BASE"]
        assert payload["conversation_id"] == conversation_id
        assert payload["agent_profile_id"] == backend.agent_profile_id
        assert payload["max_iterations"] == 160
        assert payload["tags"] == {"automationrun": str(run.id)}
        result = await execute_in_context(
            client,
            context.agent_url,
            context.session_key,
            "python3 main.py",
            b"same-bundle",
            env["WORKSPACE_BASE"],
            env,
            run_id=str(run.id),
            api_prefix=context.api_prefix,
        )
        assert result.success
        assert result.bash_command_id == "command"
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
            assert (
                requests[-1].url.path == f"/api/conversations/{conversation_id}/runtime"
            )


@pytest.mark.parametrize("explicit", [False, True])
def test_run_profile_selects_shared_backend(monkeypatch, explicit):
    from openhands.automation.backends import get_backend
    from openhands.automation.config import clear_config_cache

    selected, default = uuid4(), uuid4()
    monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", "http://server")
    monkeypatch.setenv("AUTOMATION_AGENT_PROFILE", str(default))
    clear_config_cache()
    try:
        backend = get_backend(
            AutomationRun(
                id=uuid4(),
                agent_profile_id=selected if explicit else None,
            )
        )
        assert type(backend) is ConversationAgentServerBackend
        assert backend.agent_profile_id == str(selected if explicit else default)
    finally:
        clear_config_cache()


@pytest.mark.asyncio
async def test_failed_credential_handoff_releases_runtime():
    backend = ConversationAgentServerBackend(
        "http://server",
        "host-key",
        AutomationRun(id=uuid4(), automation=Automation(name="reviewer")),
    )
    backend.agent_profile_id = str(uuid4())
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path == "/server_info":
            return httpx.Response(200, json={"conversation_runtime": "docker"})
        return httpx.Response(409 if request.url.path.endswith("credentials") else 200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await backend.get_execution_context(client)
    assert requests[-1].method == "DELETE"
    assert requests[-1].url.path.endswith("/runtime")
    with pytest.raises(RuntimeError, match="not been provisioned"):
        backend.build_env_vars()
