import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from openhands.automation.auth import AuthenticatedUser, AuthMethod
from openhands.automation.utils.model_profiles import (
    validate_agent_profile_allowlists,
)


def _request(client: httpx.AsyncClient, *, session_key: str | None = None) -> Request:
    headers = (
        [(b"x-session-api-key", session_key.encode())]
        if session_key is not None
        else [(b"authorization", b"Bearer caller-key")]
    )
    return Request(
        {
            "type": "http",
            "headers": headers,
            "app": SimpleNamespace(state=SimpleNamespace(http_client=client)),
        }
    )


def _profile_detail(profile_id: str, name: str = "hello-world", **profile):
    payload = {
        "id": profile_id,
        "name": name,
        "llm_profile_ref": "default",
    }
    payload.update(profile)
    return {"profile": payload}


@pytest.fixture
def user() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=uuid.UUID("12345678-1234-5678-1234-567812345678"),
        org_id=uuid.UUID("87654321-4321-8765-4321-876543218765"),
        email="test@example.com",
        role="owner",
        permissions=["manage_automations"],
        auth_method=AuthMethod.API_KEY,
        api_key="caller-key",
    )


@pytest.mark.asyncio
async def test_validate_agent_profile_allowlists_rejects_extra_tool(user):
    profile_id = "0a1b2c3d-0000-4000-8000-000000000001"

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/agent-profiles":
            return httpx.Response(
                200,
                json={"profiles": [{"id": profile_id, "name": "hello-world"}]},
            )
        if request.url.path == "/api/agent-profiles/hello-world":
            return httpx.Response(
                200,
                json=_profile_detail(
                    profile_id,
                    tools=[
                        {"name": "terminal", "params": {}},
                        {"name": "browser_tool_set", "params": {}},
                    ],
                ),
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(HTTPException) as exc:
            await validate_agent_profile_allowlists(
                uuid.UUID(profile_id),
                None,
                ["terminal"],
                _request(client),
                user,
            )

    assert exc.value.status_code == 422
    assert "browser_tool_set" in exc.value.detail


@pytest.mark.asyncio
async def test_validate_agent_profile_allowlists_accepts_exact_required_tools(user):
    profile_id = "0a1b2c3d-0000-4000-8000-000000000002"

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/agent-profiles":
            return httpx.Response(
                200,
                json={"profiles": [{"id": profile_id, "name": "hello-world"}]},
            )
        if request.url.path == "/api/agent-profiles/hello-world":
            return httpx.Response(
                200,
                json=_profile_detail(
                    profile_id, tools=[{"name": "terminal", "params": {}}]
                ),
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        resolved_id = await validate_agent_profile_allowlists(
            None,
            "hello-world",
            ["terminal"],
            _request(client),
            user,
        )

    assert resolved_id == uuid.UUID(profile_id)


@pytest.mark.asyncio
async def test_validate_agent_profile_allowlists_accepts_acp_summary_without_detail(
    user,
):
    profile_id = "0a1b2c3d-0000-4000-8000-000000000012"
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/agent-profiles":
            return httpx.Response(
                200,
                json={
                    "profiles": [
                        {
                            "id": profile_id,
                            "name": "codex",
                            "agent_kind": "acp",
                            "revision": 3,
                            "llm_profile_ref": None,
                            "mcp_server_refs": ["github"],
                        }
                    ]
                },
            )
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        resolved_id = await validate_agent_profile_allowlists(
            None,
            "codex",
            None,
            _request(client),
            user,
        )

    assert resolved_id == uuid.UUID(profile_id)
    assert [request.url.path for request in requests] == ["/api/agent-profiles"]


@pytest.mark.asyncio
async def test_validate_agent_profile_allowlists_rejects_missing_allowed_tool(user):
    profile_id = "0a1b2c3d-0000-4000-8000-000000000006"

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/agent-profiles":
            return httpx.Response(
                200,
                json={"profiles": [{"id": profile_id, "name": "hello-world"}]},
            )
        if request.url.path == "/api/agent-profiles/hello-world":
            return httpx.Response(200, json=_profile_detail(profile_id, tools=[]))
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(HTTPException) as exc:
            await validate_agent_profile_allowlists(
                None,
                "hello-world",
                ["terminal"],
                _request(client),
                user,
            )

    assert exc.value.status_code == 422
    assert "does not enable every tool allowed" in exc.value.detail
    assert "terminal" in exc.value.detail


@pytest.mark.asyncio
async def test_validate_agent_profile_allowlists_rejects_implicit_default_tools(
    user,
):
    profile_id = "0a1b2c3d-0000-4000-8000-000000000007"

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/agent-profiles":
            return httpx.Response(
                200,
                json={"profiles": [{"id": profile_id, "name": "hello-world"}]},
            )
        if request.url.path == "/api/agent-profiles/hello-world":
            return httpx.Response(200, json=_profile_detail(profile_id, tools=None))
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(HTTPException) as exc:
            await validate_agent_profile_allowlists(
                None,
                "hello-world",
                ["terminal"],
                _request(client),
                user,
            )

    assert exc.value.status_code == 422
    assert "must explicitly enable the tools allowed" in exc.value.detail
    assert "terminal" in exc.value.detail


@pytest.mark.asyncio
async def test_validate_agent_profile_allowlists_rejects_mcp_ref_outside_allowlist(
    user,
):
    profile_id = "0a1b2c3d-0000-4000-8000-000000000008"

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/agent-profiles":
            return httpx.Response(
                200,
                json={"profiles": [{"id": profile_id, "name": "hello-world"}]},
            )
        if request.url.path == "/api/agent-profiles/hello-world":
            return httpx.Response(
                200,
                json=_profile_detail(profile_id, mcp_server_refs=["github", "slack"]),
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(HTTPException) as exc:
            await validate_agent_profile_allowlists(
                None,
                "hello-world",
                None,
                _request(client),
                user,
                allowed_mcp_server_refs=["github"],
            )

    assert exc.value.status_code == 422
    assert "MCP server ref not allowed" in exc.value.detail
    assert "slack" in exc.value.detail


@pytest.mark.asyncio
async def test_validate_agent_profile_allowlists_rejects_missing_secret_ref(user):
    profile_id = "0a1b2c3d-0000-4000-8000-000000000009"

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/agent-profiles":
            return httpx.Response(
                200,
                json={"profiles": [{"id": profile_id, "name": "hello-world"}]},
            )
        if request.url.path == "/api/agent-profiles/hello-world":
            return httpx.Response(
                200,
                json=_profile_detail(profile_id, secret_refs=["GITHUB_TOKEN"]),
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(HTTPException) as exc:
            await validate_agent_profile_allowlists(
                None,
                "hello-world",
                None,
                _request(client),
                user,
                allowed_secret_refs=["GITHUB_TOKEN", "SLACK_BOT_TOKEN"],
            )

    assert exc.value.status_code == 422
    assert "does not enable every secret ref allowed" in exc.value.detail
    assert "SLACK_BOT_TOKEN" in exc.value.detail


@pytest.mark.asyncio
async def test_validate_agent_profile_allowlists_accepts_mcp_and_secret_allowlists(
    user,
):
    profile_id = "0a1b2c3d-0000-4000-8000-000000000010"

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/agent-profiles":
            return httpx.Response(
                200,
                json={"profiles": [{"id": profile_id, "name": "hello-world"}]},
            )
        if request.url.path == "/api/agent-profiles/hello-world":
            return httpx.Response(
                200,
                json=_profile_detail(
                    profile_id,
                    mcp_server_refs=["github"],
                    secret_refs=["GITHUB_TOKEN"],
                ),
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        resolved_id = await validate_agent_profile_allowlists(
            None,
            "hello-world",
            None,
            _request(client),
            user,
            allowed_mcp_server_refs=["github"],
            allowed_secret_refs=["GITHUB_TOKEN"],
        )

    assert resolved_id == uuid.UUID(profile_id)


@pytest.mark.asyncio
async def test_validate_agent_profile_allowlists_checks_local_profiles(
    monkeypatch, user
):
    from openhands.automation.config import clear_config_cache

    monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", "http://local-agent")
    clear_config_cache()
    requests: list[httpx.Request] = []

    profile_id = "0a1b2c3d-0000-4000-8000-000000000003"

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/agent-profiles":
            return httpx.Response(
                200,
                json={"profiles": [{"id": profile_id, "name": "hello-world-agent"}]},
            )
        if request.url.path == "/api/agent-profiles/hello-world-agent":
            return httpx.Response(
                200,
                json=_profile_detail(
                    profile_id,
                    name="hello-world-agent",
                    tools=[
                        {"name": "terminal", "params": {}},
                        {"name": "file_editor", "params": {}},
                    ],
                ),
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(HTTPException) as exc:
            await validate_agent_profile_allowlists(
                None,
                "hello-world-agent",
                ["terminal"],
                _request(client, session_key="local-key"),
                user,
            )

    clear_config_cache()
    assert exc.value.status_code == 422
    assert "file_editor" in exc.value.detail
    assert all(
        request.headers.get("X-Session-API-Key") == "local-key" for request in requests
    )


@pytest.mark.asyncio
async def test_validate_agent_profile_allowlists_rejects_missing_local_profile(
    monkeypatch, user
):
    from openhands.automation.config import clear_config_cache

    monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", "http://local-agent")
    clear_config_cache()

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/agent-profiles":
            return httpx.Response(200, json={"profiles": []})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(HTTPException) as exc:
            await validate_agent_profile_allowlists(
                None,
                "missing-profile",
                ["terminal"],
                _request(client, session_key="local-key"),
                user,
            )

    clear_config_cache()
    assert exc.value.status_code == 422
    assert "missing-profile" in exc.value.detail


@pytest.mark.asyncio
async def test_validate_agent_profile_allowlists_rejects_duplicate_names(user):
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/agent-profiles":
            return httpx.Response(
                200,
                json={
                    "profiles": [
                        {
                            "id": "0a1b2c3d-0000-4000-8000-000000000004",
                            "name": "dupe",
                        },
                        {
                            "id": "0a1b2c3d-0000-4000-8000-000000000005",
                            "name": "dupe",
                        },
                    ]
                },
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(HTTPException) as exc:
            await validate_agent_profile_allowlists(
                None,
                "dupe",
                ["terminal"],
                _request(client),
                user,
            )

    assert exc.value.status_code == 422
    assert "Duplicate agent profile names found: dupe" in exc.value.detail
