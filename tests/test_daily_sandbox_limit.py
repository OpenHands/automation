"""Daily sandbox quota (429 DAILY_SANDBOX_LIMIT_REACHED) must not be retried."""

import httpx
import pytest
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_none

from openhands.automation.backends.cloud import (
    CloudSandboxBackend,
    _daily_sandbox_limit_detail,
    _is_rate_limit_error,
)
from openhands.automation.exceptions import DailySandboxLimitReachedError
from openhands.automation.models import AutomationRun


def _response(status_code: int, body: dict) -> httpx.Response:
    request = httpx.Request("POST", "https://app.example.com/api/v1/sandboxes")
    return httpx.Response(status_code, json=body, request=request)


def test_daily_sandbox_limit_detail_matches_error_code():
    resp = _response(
        429, {"detail": {"error": "DAILY_SANDBOX_LIMIT_REACHED", "message": "nope"}}
    )
    detail = _daily_sandbox_limit_detail(resp)
    assert detail is not None
    assert detail["message"] == "nope"


def test_daily_sandbox_limit_detail_ignores_other_429s():
    resp = _response(429, {"detail": {"error": "CONCURRENCY_LIMIT_REACHED"}})
    assert _daily_sandbox_limit_detail(resp) is None
    resp = _response(200, {"detail": {"error": "DAILY_SANDBOX_LIMIT_REACHED"}})
    assert _daily_sandbox_limit_detail(resp) is None


@pytest.mark.asyncio
async def test_create_sandbox_raises_daily_limit_without_retry():
    backend = CloudSandboxBackend.__new__(CloudSandboxBackend)
    # Rebuild the retry decorator the way __init__ does, with 3 attempts.
    backend._retry = retry(
        retry=retry_if_exception(_is_rate_limit_error),
        stop=stop_after_attempt(3),
        wait=wait_none(),
        reraise=True,
    )
    run = AutomationRun()
    backend._run = run
    backend.api_url = "https://app.example.com"

    calls = 0

    def handle_request(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            429,
            request=request,
            json={
                "detail": {
                    "error": "DAILY_SANDBOX_LIMIT_REACHED",
                    "message": "Daily sandbox limit of 20 reached.",
                }
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle_request)
    ) as client:
        with pytest.raises(DailySandboxLimitReachedError, match="Daily sandbox limit"):
            await backend._create_sandbox(client, headers={})
    assert calls == 1
