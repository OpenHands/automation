"""Shared fixtures for automation service integration tests.

Required environment variables
------------------------------
OPENHANDS_API_KEY    A valid OpenHands API key (used as the Bearer token).
AUTOMATION_BASE_URL  Base URL of the deployed automation service, e.g.
                     https://automation-automations-feature.staging.all-hands.dev
"""

import os
import time
import uuid

import httpx
import pytest

# Max retries for transient 502/503/504 errors during rolling deploys
_RETRIES = 3
_RETRY_DELAY = 2  # seconds


def _require_env(name: str) -> str:
    val = os.environ.get(name, '').strip()
    if not val:
        pytest.skip(f'{name} environment variable is not set')
    return val


class _RetryTransport(httpx.HTTPTransport):
    """Automatically retries on 502/503/504 gateway errors."""

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        last_resp = None
        for attempt in range(_RETRIES + 1):
            resp = super().handle_request(request)
            if resp.status_code not in {502, 503, 504} or attempt == _RETRIES:
                return resp
            last_resp = resp
            time.sleep(_RETRY_DELAY * (attempt + 1))
        return last_resp  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Session-scoped fixtures (shared across the entire test run)
# ---------------------------------------------------------------------------


@pytest.fixture(scope='session')
def base_url() -> str:
    return _require_env('AUTOMATION_BASE_URL').rstrip('/')


@pytest.fixture(scope='session')
def api_key() -> str:
    return _require_env('OPENHANDS_API_KEY')


@pytest.fixture(scope='session')
def auth_headers(api_key: str) -> dict[str, str]:
    return {'Authorization': f'Bearer {api_key}'}


@pytest.fixture(scope='session')
def client() -> httpx.Client:
    transport = _RetryTransport(retries=0)
    with httpx.Client(timeout=30.0, transport=transport) as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEFAULT_PAYLOAD = {
    'name': 'integration-test-temp',
    'trigger': {'type': 'cron', 'schedule': '0 0 * * *', 'timezone': 'UTC'},
    'tarball_path': 'https://example.com/code.tar.gz',
    'entrypoint': 'python main.py',
}


def make_automation_payload(*, name: str | None = None, **overrides) -> dict:
    """Return a valid create-automation payload with optional overrides.

    Returns a deep copy to prevent tests from mutating shared state.
    """
    import copy

    payload = copy.deepcopy(_DEFAULT_PAYLOAD)
    payload.update(overrides)
    # Unique default name to avoid collisions in parallel runs
    if name is not None:
        payload['name'] = name
    elif 'name' not in overrides:
        payload['name'] = f'integ-{uuid.uuid4().hex[:8]}'
    return payload


# ---------------------------------------------------------------------------
# Per-test factory that auto-cleans up created automations
# ---------------------------------------------------------------------------


@pytest.fixture()
def create_automation(
    client: httpx.Client,
    base_url: str,
    auth_headers: dict[str, str],
):
    """Factory fixture: call it to create an automation; cleanup is automatic."""
    created_ids: list[str] = []

    def _create(payload: dict | None = None) -> dict:
        body = payload or make_automation_payload()
        resp = client.post(
            f'{base_url}/api/v1/automations',
            json=body,
            headers=auth_headers,
        )
        assert resp.status_code == 201, f'Create failed: {resp.text}'
        data = resp.json()
        created_ids.append(data['id'])
        return data

    yield _create

    for aid in created_ids:
        client.delete(
            f'{base_url}/api/v1/automations/{aid}',
            headers=auth_headers,
        )
