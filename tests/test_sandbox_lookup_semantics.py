"""Regression tests for sandbox lookup failure semantics (#285).

Transient sandbox-API faults must not be collapsed into "sandbox absent",
which the watchdog previously recorded as a terminal FAILED run.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from openhands.automation.utils.agent_server import VerificationResult
from openhands.automation.utils.sandbox import (
    SandboxLookupTransientError,
    get_sandbox_agent_url,
    verify_run_status,
)
from openhands.automation.watchdog import _verify_and_mark_run
from tests.test_watchdog import _create_mock_backend


def _mock_response(
    *,
    status_code: int = 200,
    json_data: object | None = None,
    request: httpx.Request | None = None,
) -> httpx.Response:
    req = request or httpx.Request("GET", "https://api.example/api/v1/sandboxes")
    return httpx.Response(status_code, json=json_data, request=req)


def _running_sandbox_payload() -> list[dict]:
    return [
        {
            "id": "sbx-1",
            "status": "RUNNING",
            "session_api_key": "session-key",
            "exposed_urls": [
                {"name": "AGENT_SERVER", "url": "https://agent.example/"},
            ],
        }
    ]


class TestGetSandboxAgentUrlSemantics:
    """Unit tests for get_sandbox_agent_url tri-state behavior."""

    @pytest.mark.asyncio
    async def test_successful_lookup_unchanged(self):
        client = AsyncMock()
        client.get = AsyncMock(
            return_value=_mock_response(json_data=_running_sandbox_payload())
        )

        result = await get_sandbox_agent_url(
            client, "https://api.example", "key", "sbx-1"
        )

        assert result == ("https://agent.example", "session-key")

    @pytest.mark.asyncio
    async def test_empty_list_means_absent(self):
        client = AsyncMock()
        client.get = AsyncMock(return_value=_mock_response(json_data=[]))

        result = await get_sandbox_agent_url(
            client, "https://api.example", "key", "sbx-missing"
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_http_404_means_absent(self):
        client = AsyncMock()
        req = httpx.Request("GET", "https://api.example/api/v1/sandboxes")
        resp = _mock_response(
            status_code=404, json_data={"detail": "not found"}, request=req
        )
        client.get = AsyncMock(return_value=resp)

        result = await get_sandbox_agent_url(
            client, "https://api.example", "key", "sbx-missing"
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_http_429_is_transient_not_absent(self):
        client = AsyncMock()
        req = httpx.Request("GET", "https://api.example/api/v1/sandboxes")
        resp = _mock_response(
            status_code=429, json_data={"detail": "rate limited"}, request=req
        )
        client.get = AsyncMock(return_value=resp)

        with pytest.raises(SandboxLookupTransientError, match="HTTP 429"):
            await get_sandbox_agent_url(client, "https://api.example", "key", "sbx-1")

    @pytest.mark.asyncio
    async def test_http_503_is_transient_not_absent(self):
        client = AsyncMock()
        req = httpx.Request("GET", "https://api.example/api/v1/sandboxes")
        resp = _mock_response(
            status_code=503, json_data={"detail": "unavailable"}, request=req
        )
        client.get = AsyncMock(return_value=resp)

        with pytest.raises(SandboxLookupTransientError, match="HTTP 503"):
            await get_sandbox_agent_url(client, "https://api.example", "key", "sbx-1")

    @pytest.mark.asyncio
    async def test_timeout_is_transient_not_absent(self):
        client = AsyncMock()
        client.get = AsyncMock(side_effect=httpx.ReadTimeout("timed out"))

        with pytest.raises(SandboxLookupTransientError, match="transport"):
            await get_sandbox_agent_url(client, "https://api.example", "key", "sbx-1")


class TestVerifyRunStatusSemantics:
    """verify_run_status must surface retryable vs absent distinctly."""

    @pytest.mark.asyncio
    async def test_absent_sandbox_is_not_retryable(self, monkeypatch):
        async def _absent(*_args, **_kwargs):
            return None

        monkeypatch.setattr(
            "openhands.automation.utils.sandbox.get_sandbox_agent_url",
            _absent,
        )

        result = await verify_run_status(
            api_url="https://api.example",
            api_key="key",
            sandbox_id="sbx-missing",
        )

        assert result.verified is False
        assert result.retryable is False
        assert result.error == "Sandbox not available"

    @pytest.mark.asyncio
    async def test_transient_429_is_retryable(self, monkeypatch):
        async def _rate_limited(*_args, **_kwargs):
            raise SandboxLookupTransientError(
                "Sandbox API temporarily unavailable (HTTP 429)"
            )

        monkeypatch.setattr(
            "openhands.automation.utils.sandbox.get_sandbox_agent_url",
            _rate_limited,
        )

        result = await verify_run_status(
            api_url="https://api.example",
            api_key="key",
            sandbox_id="sbx-1",
        )

        assert result.verified is False
        assert result.retryable is True
        assert "429" in (result.error or "")

    @pytest.mark.asyncio
    async def test_transient_timeout_is_retryable(self, monkeypatch):
        async def _timeout(*_args, **_kwargs):
            raise SandboxLookupTransientError("Sandbox API transport error: timed out")

        monkeypatch.setattr(
            "openhands.automation.utils.sandbox.get_sandbox_agent_url",
            _timeout,
        )

        result = await verify_run_status(
            api_url="https://api.example",
            api_key="key",
            sandbox_id="sbx-1",
        )

        assert result.verified is False
        assert result.retryable is True


class TestWatchdogTransientDoesNotFail:
    """Watchdog must leave RUNNING on retryable verification failures."""

    @pytest.mark.asyncio
    async def test_retryable_leaves_run_running(self, mock_settings):
        import uuid

        run = MagicMock()
        run.id = uuid.uuid4()
        run.sandbox_id = "test-sandbox-123"
        run.automation_id = uuid.uuid4()

        verification = VerificationResult(
            verified=False,
            error="Sandbox API temporarily unavailable (HTTP 429)",
            retryable=True,
        )
        mock_backend = _create_mock_backend(verification)
        session = AsyncMock()

        with (
            patch(
                "openhands.automation.watchdog.get_backend",
                return_value=mock_backend,
            ),
            patch(
                "openhands.automation.watchdog._get_automation_keep_alive",
                AsyncMock(return_value=False),
            ),
        ):
            marked = await _verify_and_mark_run(session, run, mock_settings)

        assert marked is False
        session.execute.assert_not_called()
        mock_backend.cleanup_after_verification.assert_not_called()

    @pytest.mark.asyncio
    async def test_absent_sandbox_still_fails(self, mock_settings):
        import uuid

        run = MagicMock()
        run.id = uuid.uuid4()
        run.sandbox_id = "test-sandbox-123"
        run.automation_id = uuid.uuid4()
        run.timeout_at = None

        verification = VerificationResult(
            verified=False,
            error="Sandbox not available",
            retryable=False,
        )
        mock_backend = _create_mock_backend(verification)
        session = AsyncMock()
        execute_result = MagicMock()
        execute_result.rowcount = 1
        session.execute = AsyncMock(return_value=execute_result)

        with (
            patch(
                "openhands.automation.watchdog.get_backend",
                return_value=mock_backend,
            ),
            patch(
                "openhands.automation.watchdog._get_automation_keep_alive",
                AsyncMock(return_value=False),
            ),
            patch(
                "openhands.automation.watchdog._loaded_automation",
                return_value=None,
            ),
            patch(
                "openhands.automation.watchdog.capture_automation_event",
                AsyncMock(),
            ),
        ):
            marked = await _verify_and_mark_run(session, run, mock_settings)

        assert marked is True
        session.execute.assert_called_once()
        mock_backend.cleanup_after_verification.assert_called_once()
