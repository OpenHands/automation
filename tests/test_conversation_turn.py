"""Tests for the one request the service makes to a conversation it did not open.

`POST /api/conversations/{id}/events` with `run: true` is the agent-server
behaviour `continue_conversation` is built on -- append a message, and start the
agent loop only if it is not already running. These tests pin that request.
"""

import json
from types import SimpleNamespace
from typing import cast

import httpx
import pytest

from openhands.automation.models import AutomationRun
from openhands.automation.utils import conversation_turn as turn_module
from openhands.automation.utils.conversation_turn import send_conversation_turn


def fake_httpx(handler) -> SimpleNamespace:
    """Stand in for the module's `httpx`, serving `handler` to every request."""

    def client(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    return SimpleNamespace(AsyncClient=client)


def local_backend(agent_url="https://local-agent.example.com"):
    class FakeBackend:
        is_local_mode = True

        async def get_execution_context(self, client):
            return SimpleNamespace(agent_url=agent_url, session_key="local-key")

    return FakeBackend()


def cloud_backend():
    class FakeBackend:
        is_local_mode = False

        async def get_api_key(self):
            return "cloud-key"

    return FakeBackend()


def make_run(sandbox_id: str | None = None) -> AutomationRun:
    return cast(AutomationRun, SimpleNamespace(id="run-1", sandbox_id=sandbox_id))


@pytest.mark.asyncio
async def test_a_turn_is_a_user_message_that_starts_the_loop(monkeypatch):
    seen: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["key"] = request.headers.get("X-Session-API-Key")
        seen["body"] = request.read()
        return httpx.Response(200, json={"success": True})

    monkeypatch.setattr(turn_module, "get_backend", lambda run: local_backend())
    monkeypatch.setattr(turn_module, "httpx", fake_httpx(handler))

    assert await send_conversation_turn(make_run(), "conv-1", "another turn") is True

    body = json.loads(seen["body"])
    assert seen["path"] == "/api/conversations/conv-1/events"
    assert seen["key"] == "local-key"
    assert body["role"] == "user"
    assert body["content"] == [{"type": "text", "text": "another turn"}]
    # Without this the message lands in history and nothing answers it.
    assert body["run"] is True


@pytest.mark.asyncio
async def test_a_cloud_run_is_reached_through_its_sandbox(monkeypatch):
    seen: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["host"] = request.url.host
        seen["key"] = request.headers.get("X-Session-API-Key")
        return httpx.Response(200, json={"success": True})

    async def fake_get_sandbox_agent_url(client, api_url, api_key, sandbox_id):
        seen["sandbox_id"] = sandbox_id
        return "https://sandbox.example.com", "sandbox-key"

    monkeypatch.setattr(turn_module, "get_backend", lambda run: cloud_backend())
    monkeypatch.setattr(
        turn_module, "get_sandbox_agent_url", fake_get_sandbox_agent_url
    )
    monkeypatch.setattr(turn_module, "httpx", fake_httpx(handler))

    assert await send_conversation_turn(make_run("sbx-1"), "conv-1", "hi") is True
    assert seen["sandbox_id"] == "sbx-1"
    assert seen["host"] == "sandbox.example.com"
    assert seen["key"] == "sandbox-key"


@pytest.mark.asyncio
async def test_a_cloud_run_with_no_sandbox_never_sends(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not have been called")

    monkeypatch.setattr(turn_module, "get_backend", lambda run: cloud_backend())
    monkeypatch.setattr(turn_module, "httpx", fake_httpx(handler))

    assert await send_conversation_turn(make_run(None), "conv-1", "hi") is False


@pytest.mark.asyncio
async def test_a_reaped_sandbox_is_a_false_not_an_exception(monkeypatch):
    """The caller answers False by starting a run, which is the right answer."""

    async def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not have been called")

    async def fake_get_sandbox_agent_url(client, api_url, api_key, sandbox_id):
        return None

    monkeypatch.setattr(turn_module, "get_backend", lambda run: cloud_backend())
    monkeypatch.setattr(
        turn_module, "get_sandbox_agent_url", fake_get_sandbox_agent_url
    )
    monkeypatch.setattr(turn_module, "httpx", fake_httpx(handler))

    assert await send_conversation_turn(make_run("sbx-1"), "conv-1", "hi") is False


@pytest.mark.asyncio
async def test_a_missing_conversation_is_a_false(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Item not found"})

    monkeypatch.setattr(turn_module, "get_backend", lambda run: local_backend())
    monkeypatch.setattr(turn_module, "httpx", fake_httpx(handler))

    assert await send_conversation_turn(make_run(), "conv-gone", "hi") is False


@pytest.mark.asyncio
async def test_a_transport_failure_is_a_false(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(turn_module, "get_backend", lambda run: local_backend())
    monkeypatch.setattr(turn_module, "httpx", fake_httpx(handler))

    assert await send_conversation_turn(make_run(), "conv-1", "hi") is False
