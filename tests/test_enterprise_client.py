"""Tests for the enterprise service API client."""

import uuid

import httpx
import pytest

from openhands.automation.config import clear_config_cache
from openhands.automation.utils.enterprise_client import (
    EnterpriseClientError,
    OrgMemberInfo,
    get_org_member,
)


USER_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
ORG_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


@pytest.fixture(autouse=True)
def cloud_mode(monkeypatch):
    monkeypatch.delenv("AUTOMATION_AGENT_SERVER_URL", raising=False)
    monkeypatch.setenv("AUTOMATION_SERVICE_KEY", "test-service-key")
    monkeypatch.setenv("AUTOMATION_OPENHANDS_API_BASE_URL", "https://enterprise.test")
    clear_config_cache()
    yield
    clear_config_cache()


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_returns_typed_member_and_sends_the_service_key():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "user_id": str(USER_ID),
                "org_id": str(ORG_ID),
                "account_type": "service",
                "role": "service_account",
                "status": "active",
            },
        )

    async with _client(handler) as client:
        member = await get_org_member(client, ORG_ID, USER_ID)

    assert member == OrgMemberInfo(
        user_id=USER_ID,
        org_id=ORG_ID,
        account_type="service",
        role="service_account",
        status="active",
    )
    assert str(seen[0].url) == (
        f"https://enterprise.test/api/service/users/{USER_ID}/orgs/{ORG_ID}/membership"
    )
    assert seen[0].headers["X-Service-API-Key"] == "test-service-key"


async def test_404_means_not_a_member():
    async with _client(lambda request: httpx.Response(404)) as client:
        assert await get_org_member(client, ORG_ID, USER_ID) is None


@pytest.mark.parametrize("status_code", [401, 500, 503])
async def test_other_statuses_raise(status_code):
    async with _client(lambda request: httpx.Response(status_code)) as client:
        with pytest.raises(EnterpriseClientError):
            await get_org_member(client, ORG_ID, USER_ID)


async def test_unexpected_body_raises():
    async with _client(
        lambda request: httpx.Response(200, json={"account_type": "robot"})
    ) as client:
        with pytest.raises(EnterpriseClientError):
            await get_org_member(client, ORG_ID, USER_ID)


async def test_transport_failure_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    async with _client(handler) as client:
        with pytest.raises(EnterpriseClientError):
            await get_org_member(client, ORG_ID, USER_ID)


async def test_missing_service_key_raises(monkeypatch):
    monkeypatch.setenv("AUTOMATION_SERVICE_KEY", "")
    clear_config_cache()

    async with _client(lambda request: httpx.Response(200)) as client:
        with pytest.raises(EnterpriseClientError):
            await get_org_member(client, ORG_ID, USER_ID)


async def test_local_mode_returns_none_without_a_request(monkeypatch):
    monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", "http://localhost:3000")
    clear_config_cache()

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("local mode must not call enterprise")

    async with _client(handler) as client:
        assert await get_org_member(client, ORG_ID, USER_ID) is None
