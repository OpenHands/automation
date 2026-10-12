import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from openhands.automation.auth import AuthenticatedUser, AuthMethod
from openhands.automation.utils.model_profiles import (
    validate_agent_profile_allowed_tools,
)


def _request(
    client: httpx.AsyncClient, *, session_key: str | None = None
) -> Request:
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
async def test_validate_agent_profile_allowed_tools_rejects_extra_tool(user):
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
                json={
                    "name": "hello-world",
                    "profile": {
                        "tools": [
                            {"name": "terminal", "params": {}},
                            {"name": "browser_tool_set", "params": {}},
                        ]
                    },
                },
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(HTTPException) as exc:
            await validate_agent_profile_allowed_tools(
                uuid.UUID(profile_id),
                None,
                ["terminal", "file_editor", "task_tracker"],
                _request(client),
                user,
            )

    assert exc.value.status_code == 422
    assert "browser_tool_set" in exc.value.detail


@pytest.mark.asyncio
async def test_validate_agent_profile_allowed_tools_accepts_subset(user):
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/agent-profiles":
            return httpx.Response(200, json={"profiles": []})
        if request.url.path == "/api/agent-profiles/hello-world":
            return httpx.Response(
                200,
                json={
                    "name": "hello-world",
                    "profile": {"tools": [{"name": "terminal", "params": {}}]},
                },
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await validate_agent_profile_allowed_tools(
            None,
            "hello-world",
            ["terminal", "file_editor", "task_tracker"],
            _request(client),
            user,
        )


@pytest.mark.asyncio
async def test_validate_agent_profile_allowed_tools_checks_local_profiles(
    monkeypatch, user
):
    from openhands.automation.config import clear_config_cache

    monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", "http://local-agent")
    clear_config_cache()
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/agent-profiles":
            return httpx.Response(200, json={"profiles": []})
        if request.url.path == "/api/agent-profiles/hello-world-agent":
            return httpx.Response(
                200,
                json={
                    "name": "hello-world-agent",
                    "profile": {
                        "tools": [
                            {"name": "terminal", "params": {}},
                            {"name": "file_editor", "params": {}},
                        ]
                    },
                },
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(HTTPException) as exc:
            await validate_agent_profile_allowed_tools(
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
        request.headers.get("X-Session-API-Key") == "local-key"
        for request in requests
    )


@pytest.mark.asyncio
async def test_validate_agent_profile_allowed_tools_rejects_missing_local_profile(
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
            await validate_agent_profile_allowed_tools(
                None,
                "missing-profile",
                ["terminal"],
                _request(client, session_key="local-key"),
                user,
            )

    clear_config_cache()
    assert exc.value.status_code == 422
    assert "missing-profile" in exc.value.detail
