from uuid import uuid4

import httpx
import pytest

from openhands.automation.backends.docker import DockerAgentServerBackend
from openhands.automation.execution import execute_in_context
from openhands.automation.models import Automation, AutomationRun


@pytest.mark.asyncio
async def test_docker_dispatch_scopes_uploads_and_omits_host_credentials():
    run = AutomationRun(id=uuid4(), automation=Automation(name="factory"))
    backend = DockerAgentServerBackend(
        "http://server",
        "host-control-key",
        run,
        callback_api_key="shared-automation-key",
    )
    backend.agent_profile_id = str(uuid4())
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200, json={"id": "command-123", "session_api_key": "runtime-only-key"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        ctx = await backend.get_execution_context(client)
        env = backend.build_env_vars()
        assert "host-control-key" not in str(env)
        assert "shared-automation-key" not in str(env)
        assert env["AGENT_SERVER_URL"] == "http://127.0.0.1:8000"
        result = await execute_in_context(
            client,
            ctx.agent_url,
            ctx.session_key,
            "python3 main.py",
            b"tarball",
            backend.get_work_dir(str(run.id)),
            env,
            run_id=str(run.id),
            api_prefix=ctx.api_prefix,
        )
        assert result.success
        assert result.bash_command_id == "command-123"
        for request in requests[2:]:
            assert request.url.path.startswith(f"/api/conversations/{run.id}/")
            if request.url.path.endswith("/file/upload"):
                assert request.url.params["path"].startswith("/workspace/")
        await backend.release_context(client, ctx)
        assert requests[-1].method == "DELETE"
        assert requests[-1].url.path == f"/api/conversations/{run.id}/runtime"


@pytest.mark.asyncio
async def test_failed_credential_handoff_releases_runtime():
    run = AutomationRun(id=uuid4(), automation=Automation(name="factory"))
    backend = DockerAgentServerBackend("http://server", "host-key", run)
    backend.agent_profile_id = str(uuid4())
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(409 if request.url.path.endswith("credentials") else 200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await backend.get_execution_context(client)
    assert requests[-1].method == "DELETE"
    assert requests[-1].url.path == f"/api/conversations/{run.id}/runtime"
    with pytest.raises(RuntimeError, match="not been provisioned"):
        backend.build_env_vars()


def test_host_configuration_selects_a_profile_for_each_automation(monkeypatch):
    import json

    from openhands.automation.backends import get_backend
    from openhands.automation.config import clear_config_cache

    automation_id = uuid4()
    selected = str(uuid4())
    default = str(uuid4())
    monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", "http://server")
    monkeypatch.setenv("AUTOMATION_DOCKER_AGENT_PROFILE", default)
    monkeypatch.setenv(
        "AUTOMATION_DOCKER_AGENT_PROFILE_OVERRIDES",
        json.dumps({str(automation_id): selected}),
    )
    clear_config_cache()
    try:
        scoped = get_backend(AutomationRun(id=uuid4(), automation_id=automation_id))
        ordinary = get_backend(AutomationRun(id=uuid4(), automation_id=uuid4()))
        assert isinstance(scoped, DockerAgentServerBackend)
        assert isinstance(ordinary, DockerAgentServerBackend)
        assert scoped.agent_profile_id == selected
        assert ordinary.agent_profile_id == default
    finally:
        clear_config_cache()
