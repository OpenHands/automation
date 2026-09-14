"""Tests for the request that continues a conversation."""

import json
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest

from openhands.automation.backends.conversation import ConversationBackend
from openhands.automation.backends.local import LocalAgentServerBackend
from openhands.automation.models import Automation, AutomationRun
from openhands.automation.utils import conversation_turn as turn_module, utcnow
from openhands.automation.utils.conversation_turn import (
    _submit_turn,
    compose_turn,
    send_conversation_turn,
)


CONVERSATION_ID = str(uuid4())


def test_service_owned_turn_uses_the_control_client(monkeypatch):
    from unittest.mock import MagicMock

    workspace = MagicMock()
    remote_workspace = MagicMock(return_value=workspace)
    control = MagicMock()
    remote_control = MagicMock(return_value=control)
    monkeypatch.setattr(turn_module, "RemoteWorkspace", remote_workspace)
    monkeypatch.setattr(turn_module, "RemoteConversationControl", remote_control)

    _submit_turn(
        "https://agent.example.com",
        "outer-key",
        CONVERSATION_ID,
        "work on this",
        True,
    )

    remote_workspace.assert_called_once_with(
        host="https://agent.example.com", api_key="outer-key", working_dir="/"
    )
    remote_control.assert_called_once_with(workspace, UUID(CONVERSATION_ID))
    control.send_message.assert_called_once_with("work on this", run=True)


@pytest.fixture
def conversation_transport(sdk_http_transport):
    def install(handler):
        requests = []

        def respond(request):
            requests.append(request)
            return handler(request)

        sdk_http_transport(respond)
        return requests

    return install


def local_backend(
    agent_url="https://local-agent.example.com", *, conversation_runtime=False
):
    run = AutomationRun(
        id=uuid4(),
        automation=Automation(id=uuid4(), org_id=uuid4(), name="test"),
        agent_profile_id=uuid4(),
        execution_scope="conversation" if conversation_runtime else "run",
    )
    server = LocalAgentServerBackend(agent_url, "local-key", run)
    if conversation_runtime:
        return ConversationBackend(server, run)
    return server


def cloud_backend():
    class FakeBackend:
        is_local_mode = False

        async def get_api_key(self):
            return "cloud-key"

    return FakeBackend()


def make_run(sandbox_id: str | None = None, *, finished: bool = False) -> AutomationRun:
    return cast(
        AutomationRun,
        SimpleNamespace(
            id="run-1",
            sandbox_id=sandbox_id,
            completed_at=utcnow() if finished else None,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("conversation_runtime", [False, True])
async def test_a_turn_is_a_user_message_that_starts_the_loop(
    monkeypatch, conversation_runtime, conversation_transport
):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["key"] = request.headers.get("X-Session-API-Key")
        seen["body"] = request.read()
        return httpx.Response(200, json={"success": True})

    monkeypatch.setattr(
        turn_module,
        "get_backend",
        lambda run: local_backend(conversation_runtime=conversation_runtime),
    )
    requests = conversation_transport(handler)

    assert (
        await send_conversation_turn(make_run(), CONVERSATION_ID, "another turn")
        is True
    )

    body = json.loads(seen["body"])
    assert seen["path"] == f"/api/conversations/{CONVERSATION_ID}/events"
    assert seen["key"] == "local-key"
    assert body.get("role", "user") == "user"
    assert [(c["type"], c["text"]) for c in body["content"]] == [
        ("text", "another turn")
    ]
    assert body["run"] is True
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_wake_agent_false_appends_without_starting_the_loop(
    monkeypatch, conversation_transport
):
    """A trigger that buffers rather than interrupts.

    The turn still has to reach the agent server -- the conversation is what
    holds it, in order, until the script decides to act. Only `run` changes.
    """
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.read()
        return httpx.Response(200, json={"success": True})

    monkeypatch.setattr(turn_module, "get_backend", lambda run: local_backend())
    requests = conversation_transport(handler)

    delivered = await send_conversation_turn(
        make_run(), CONVERSATION_ID, "for later", wake_agent=False
    )

    assert delivered is True
    body = json.loads(seen["body"])
    assert body["run"] is False
    assert len(requests) == 1
    assert [(c["type"], c["text"]) for c in body["content"]] == [("text", "for later")]


@pytest.mark.asyncio
async def test_a_cloud_run_is_reached_through_its_sandbox(
    monkeypatch, conversation_transport
):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
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
    conversation_transport(handler)

    assert (
        await send_conversation_turn(make_run("sbx-1"), CONVERSATION_ID, "hi") is True
    )
    assert seen["sandbox_id"] == "sbx-1"
    assert seen["host"] == "sandbox.example.com"
    assert seen["key"] == "sandbox-key"


@pytest.mark.asyncio
async def test_a_cloud_run_with_no_sandbox_never_sends(
    monkeypatch, conversation_transport
):
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not have been called")

    monkeypatch.setattr(turn_module, "get_backend", lambda run: cloud_backend())
    conversation_transport(handler)

    assert await send_conversation_turn(make_run(None), CONVERSATION_ID, "hi") is False


@pytest.mark.asyncio
async def test_a_reaped_sandbox_is_a_false_not_an_exception(
    monkeypatch, conversation_transport
):
    """The caller answers a False by starting a run."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not have been called")

    async def fake_get_sandbox_agent_url(client, api_url, api_key, sandbox_id):
        return None

    async def fake_resume_sandbox(client, api_url, api_key, sandbox_id):
        # 404 from the resume: the sandbox is gone, not merely paused.
        return False

    monkeypatch.setattr(turn_module, "get_backend", lambda run: cloud_backend())
    monkeypatch.setattr(
        turn_module, "get_sandbox_agent_url", fake_get_sandbox_agent_url
    )
    monkeypatch.setattr(turn_module, "resume_sandbox", fake_resume_sandbox)
    conversation_transport(handler)

    assert (
        await send_conversation_turn(make_run("sbx-1"), CONVERSATION_ID, "hi") is False
    )


@pytest.mark.asyncio
async def test_a_paused_sandbox_is_resumed_rather_than_abandoned(
    monkeypatch, conversation_transport
):
    """An idle sandbox is paused, not deleted -- the conversation survives it.

    Falling straight back to a run here would start a second conversation and
    the thread would silently lose its memory every time it went idle.
    """
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"success": True})

    async def fake_get_sandbox_agent_url(client, api_url, api_key, sandbox_id):
        # Paused on the first look, RUNNING once the resume has landed.
        if "resumed" in calls:
            return "https://agent.example.com", "sk-1"
        return None

    async def fake_resume_sandbox(client, api_url, api_key, sandbox_id):
        calls.append("resumed")
        return True

    monkeypatch.setattr(turn_module, "get_backend", lambda run: cloud_backend())
    monkeypatch.setattr(
        turn_module, "get_sandbox_agent_url", fake_get_sandbox_agent_url
    )
    monkeypatch.setattr(turn_module, "resume_sandbox", fake_resume_sandbox)
    conversation_transport(handler)
    monkeypatch.setattr(turn_module, "RESUME_POLL_SECONDS", 0)

    assert (
        await send_conversation_turn(make_run("sbx-1"), CONVERSATION_ID, "hi") is True
    )
    assert "resumed" in calls
    assert f"/api/conversations/{CONVERSATION_ID}/events" in calls


@pytest.mark.asyncio
async def test_a_resume_that_never_comes_back_gives_up(
    monkeypatch, conversation_transport
):
    """Past the budget the caller starts a run, as it did before."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not have been called")

    async def fake_get_sandbox_agent_url(client, api_url, api_key, sandbox_id):
        return None

    async def fake_resume_sandbox(client, api_url, api_key, sandbox_id):
        return True

    monkeypatch.setattr(turn_module, "get_backend", lambda run: cloud_backend())
    monkeypatch.setattr(
        turn_module, "get_sandbox_agent_url", fake_get_sandbox_agent_url
    )
    monkeypatch.setattr(turn_module, "resume_sandbox", fake_resume_sandbox)
    conversation_transport(handler)
    monkeypatch.setattr(turn_module, "RESUME_POLL_SECONDS", 0)
    monkeypatch.setattr(turn_module, "RESUME_WAIT_SECONDS", 0)

    assert (
        await send_conversation_turn(make_run("sbx-1"), CONVERSATION_ID, "hi") is False
    )


@pytest.mark.asyncio
async def test_a_missing_conversation_is_a_false(monkeypatch, conversation_transport):
    """A finished run's conversation is gone for good; do not wait on it."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404, json={"detail": "Item not found"})

    monkeypatch.setattr(turn_module, "get_backend", lambda run: local_backend())
    conversation_transport(handler)

    run = make_run(finished=True)
    assert await send_conversation_turn(run, CONVERSATION_ID, "hi") is False
    assert calls == 1


@pytest.mark.asyncio
async def test_a_conversation_still_opening_is_waited_for(
    monkeypatch, conversation_transport
):
    """The sandbox answers before the script has opened the conversation.

    Giving up on that 404 is what forked a second run for an event arriving
    mid-startup, so the append is retried while the run is still going.
    """
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(404, json={"detail": "Item not found"})
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(turn_module, "get_backend", lambda run: local_backend())
    conversation_transport(handler)
    monkeypatch.setattr(turn_module, "CONVERSATION_POLL_SECONDS", 0)

    assert await send_conversation_turn(make_run(), CONVERSATION_ID, "hi") is True
    assert calls == 3


@pytest.mark.asyncio
async def test_waiting_for_a_conversation_is_bounded(
    monkeypatch, conversation_transport
):
    """A conversation that never opens still degrades to a run."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Item not found"})

    monkeypatch.setattr(turn_module, "get_backend", lambda run: local_backend())
    conversation_transport(handler)
    monkeypatch.setattr(turn_module, "CONVERSATION_POLL_SECONDS", 0)
    monkeypatch.setattr(turn_module, "CONVERSATION_WAIT_SECONDS", 0)

    assert await send_conversation_turn(make_run(), CONVERSATION_ID, "hi") is False


@pytest.mark.asyncio
async def test_a_transport_failure_is_a_false(monkeypatch, conversation_transport):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(turn_module, "get_backend", lambda run: local_backend())
    conversation_transport(handler)

    assert await send_conversation_turn(make_run(), CONVERSATION_ID, "hi") is False


def test_a_specific_nested_path_beats_a_generic_top_level_one():
    """`_BODY_PATHS` is ordered most-specific-first, and the order must win.

    A custom webhook wrapping the real event under `payload` also carries a
    top-level `text`; rooting the search first would return that instead.
    """
    turn = compose_turn(
        "custom",
        "thing.happened",
        {"text": "ok", "payload": {"comment": {"body": "the real message"}}},
    )

    assert "the real message" in turn
    assert "ok" not in turn


def test_a_pull_request_is_not_described_as_a_comment():
    """`pull_request.body` matches the body paths, but nobody commented."""
    turn = compose_turn(
        "github",
        "pull_request.opened",
        {
            "repository": {"full_name": "org/repo"},
            "pull_request": {
                "number": 5,
                "body": "please review",
                "user": {"login": "alice"},
            },
        },
    )

    assert "commented on" not in turn
    assert "pull_request.opened" in turn
    assert "org/repo#5" in turn
    assert "please review" in turn


def test_a_comment_still_reads_as_a_comment():
    turn = compose_turn(
        "github",
        "issue_comment.created",
        {
            "repository": {"full_name": "org/repo"},
            "issue": {"number": 5},
            "comment": {
                "body": "what about the tests?",
                "user": {"login": "alice"},
                "html_url": "https://github.com/org/repo/issues/5#issuecomment-1",
            },
        },
    )

    assert "@alice commented on org/repo#5" in turn
    assert "what about the tests?" in turn
    # The link survives now that the schema keeps html_url.
    assert "https://github.com/org/repo/issues/5#issuecomment-1" in turn
