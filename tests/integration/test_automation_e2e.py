"""End-to-end integration tests for automation dispatch.

These tests require a live OpenHands Cloud environment and a valid API key.
They are skipped by default — run them explicitly with::

    OPENHANDS_API_KEY=sk-oh-... pytest tests/integration/ -m integration

Or via the standalone script::

    python scripts/test_automation.py --api-url https://staging.all-hands.dev
"""

import os
import sys

import pytest

# Allow importing from scripts/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

integration = pytest.mark.skipif(
    not os.environ.get("OPENHANDS_API_KEY"),
    reason="Set OPENHANDS_API_KEY to run integration tests",
)


@integration
@pytest.mark.asyncio
async def test_sandbox_lifecycle():
    """Verify full sandbox lifecycle: create → upload → execute → delete."""
    from scripts.test_automation import run_test

    api_url = os.environ.get("OPENHANDS_API_URL", "https://staging.all-hands.dev")
    api_key = os.environ["OPENHANDS_API_KEY"]
    ok = await run_test(api_url, api_key)
    assert ok, "Sandbox lifecycle test failed"
