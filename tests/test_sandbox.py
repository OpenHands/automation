"""Tests for sandbox verification utilities."""

import httpx
import pytest

from openhands.automation.config import clear_config_cache
from openhands.automation.utils.agent_server import VerificationOutcome
from openhands.automation.utils.sandbox import (
    SandboxApiTransientError,
    find_agent_server_url,
    get_sandbox_agent_url,
    pause_sandbox,
    verify_run_status,
)


@pytest.fixture
def sandbox_internal_url(monkeypatch):
    """Set AUTOMATION_SANDBOX_INTERNAL_URL for one test."""

    def set_url(value: str) -> None:
        monkeypatch.setenv("AUTOMATION_SANDBOX_INTERNAL_URL", value)
        clear_config_cache()

    monkeypatch.delenv("AUTOMATION_SANDBOX_INTERNAL_URL", raising=False)
    clear_config_cache()
    yield set_url
    clear_config_cache()


def _sandbox(url: str) -> dict:
    return {
        "exposed_urls": [
            {"name": "OTHER", "url": "http://other.example.com"},
            {"name": "AGENT_SERVER", "url": url},
        ],
        "session_api_key": "session-key",
    }


def test_find_agent_server_url_found(sandbox_internal_url):
    result = find_agent_server_url(_sandbox("http://agent.example.com/"))
    assert result == ("http://agent.example.com", "session-key")


def test_find_agent_server_url_not_found(sandbox_internal_url):
    sandbox = {"exposed_urls": [{"name": "OTHER", "url": "http://other.example.com"}]}
    assert find_agent_server_url(sandbox) is None


def test_find_agent_server_url_empty(sandbox_internal_url):
    assert find_agent_server_url({"exposed_urls": None}) is None


def test_find_agent_server_url_unchanged_without_internal_url(sandbox_internal_url):
    url = "http://localhost:9043/runtime/oh-agent-server-abc/8000"
    assert find_agent_server_url(_sandbox(url)) == (url, "session-key")


@pytest.mark.parametrize("internal_url", ["http://proxy", "http://proxy/"])
def test_find_agent_server_url_uses_internal_url(sandbox_internal_url, internal_url):
    sandbox_internal_url(internal_url)
    result = find_agent_server_url(
        _sandbox("http://localhost:9043/runtime/oh-agent-server-abc/8000")
    )
    assert result == ("http://proxy/runtime/oh-agent-server-abc/8000", "session-key")


def test_find_agent_server_url_replaces_custom_domain(sandbox_internal_url):
    sandbox_internal_url("http://proxy")
    result = find_agent_server_url(
        _sandbox("https://openhands.example.com/runtime/oh-agent-server-abc/8000/?a=1")
    )
    assert result == (
        "http://proxy/runtime/oh-agent-server-abc/8000/?a=1",
        "session-key",
    )


@pytest.mark.asyncio
async def test_get_sandbox_agent_url_uses_internal_url(sandbox_internal_url):
    sandbox_internal_url("http://proxy")

    def handler(request: httpx.Request) -> httpx.Response:
        sandbox = _sandbox("http://localhost:9043/runtime/oh-agent-server-abc/8000")
        return httpx.Response(200, json=[{**sandbox, "status": "RUNNING"}])

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await get_sandbox_agent_url(
            client, "https://app.example.com", "test-key", "sandbox-123"
        )
    assert result == ("http://proxy/runtime/oh-agent-server-abc/8000", "session-key")


@pytest.mark.asyncio
async def test_get_sandbox_agent_url_raises_transient_on_rate_limit():
    """HTTP 429 from sandbox API is a transient check failure, not absence."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"detail": "rate limited"}, request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(SandboxApiTransientError, match="HTTP 429"):
            await get_sandbox_agent_url(
                client,
                "https://app.example.com",
                "test-key",
                "sandbox-123",
            )


@pytest.mark.asyncio
async def test_get_sandbox_agent_url_raises_transient_on_transport_timeout():
    """Transport timeouts do not prove the sandbox is absent."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connect timed out", request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(SandboxApiTransientError, match="connect timed out"):
            await get_sandbox_agent_url(
                client,
                "https://app.example.com",
                "test-key",
                "sandbox-123",
            )


@pytest.mark.asyncio
async def test_get_sandbox_agent_url_returns_none_for_successful_empty_lookup():
    """A successful empty sandbox lookup still means unavailable."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[], request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await get_sandbox_agent_url(
            client,
            "https://app.example.com",
            "test-key",
            "sandbox-123",
        )

    assert result is None


@pytest.mark.asyncio
async def test_verify_run_status_marks_rate_limit_as_transient(monkeypatch):
    """verify_run_status preserves transient sandbox API failures for callers."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"detail": "rate limited"}, request=request)

    transport = httpx.MockTransport(handler)

    class TransportAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(
        "openhands.automation.utils.sandbox.httpx.AsyncClient",
        TransportAsyncClient,
    )

    result = await verify_run_status(
        api_url="https://app.example.com",
        api_key="test-key",
        sandbox_id="sandbox-123",
        run_id="run-123",
    )

    assert result.outcome == VerificationOutcome.TRANSIENT_ERROR
    assert result.verified is False
    assert result.transient is True
    assert result.error is not None and "HTTP 429" in result.error
    assert result.error_info is not None
    assert result.error_info.fingerprint == "sandbox_api:get_sandbox:rate_limited:429"


def _accept(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"success": True}, request=request)


def _not_found(request: httpx.Request) -> httpx.Response:
    return httpx.Response(404, json={"detail": "Not Found"}, request=request)


def _refuse(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("respond", "expected"),
    [(_accept, True), (_not_found, False), (_refuse, False)],
)
async def test_pause_sandbox_reports_whether_the_api_accepted(
    monkeypatch, respond, expected
):
    """pause_sandbox is best-effort: a non-2xx or a transport failure is False."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return respond(request)

    transport = httpx.MockTransport(handler)

    class TransportAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(
        "openhands.automation.utils.sandbox.httpx.AsyncClient",
        TransportAsyncClient,
    )

    result = await pause_sandbox(
        api_url="https://app.example.com/",
        api_key="test-key",
        sandbox_id="sandbox-123",
        run_id="run-123",
    )

    assert result is expected
    assert seen[0].method == "POST"
    assert (
        str(seen[0].url) == "https://app.example.com/api/v1/sandboxes/sandbox-123/pause"
    )
    assert seen[0].headers["Authorization"] == "Bearer test-key"
