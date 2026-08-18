"""Tests for sandbox verification utilities."""

import httpx
import pytest

from openhands.automation.utils.agent_server import VerificationOutcome
from openhands.automation.utils.sandbox import (
    SandboxApiTransientError,
    get_sandbox_agent_url,
    verify_run_status,
)


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
