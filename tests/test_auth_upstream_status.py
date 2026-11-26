"""What a caller sees for each answer of OpenHands ``GET /api/v1/users/me``.

Requests go through the app and the real ``authenticate_request``; only the
OpenHands API is stubbed, at its HTTP boundary (``httpx.MockTransport``).
"""

import uuid
from collections.abc import AsyncGenerator

import httpx
import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient

from openhands.automation.app import app
from openhands.automation.config import clear_config_cache
from openhands.automation.db import get_session
from openhands.automation.utils.change_events import bind_request


ORG_ID = uuid.uuid4()
OTHER_ORG_ID = uuid.uuid4()
NOT_AUTHORIZED = "API key is not authorized for this organization"


class UsersMeApi:
    """The OpenHands API's ``/api/v1/users/me``, answering with ``answer``."""

    def __init__(self) -> None:
        self.answer: httpx.Response | Exception = self.member(ORG_ID)
        self.requests: list[httpx.Request] = []
        self.client: AsyncClient

    @staticmethod
    def member(org_id: uuid.UUID) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": str(uuid.uuid4()),
                "org_id": str(org_id),
                "email": "user@example.com",
                "role": "member",
                "permissions": ["view_automations"],
            },
        )

    def respond(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/users/me"
        self.requests.append(request)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture
async def users_me(
    async_engine, async_session_factory, async_session, monkeypatch
) -> AsyncGenerator[UsersMeApi, None]:
    """Cloud mode with real authentication against a stubbed OpenHands API."""
    monkeypatch.delenv("AUTOMATION_AGENT_SERVER_URL", raising=False)
    clear_config_cache()

    async def override_get_session(request: Request):
        bind_request(async_session, request)
        yield async_session

    app.dependency_overrides[get_session] = override_get_session
    app.state.engine = async_engine
    app.state.session_factory = async_session_factory
    api = UsersMeApi()
    app.state.http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(api.respond)
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer sk-oh-test", "X-Org-Id": str(OTHER_ORG_ID)},
    ) as client:
        api.client = client
        yield api
    await app.state.http_client.aclose()
    app.dependency_overrides.clear()
    clear_config_cache()


ROUTES = [
    "/api/automation/v1?limit=1",
    f"/api/automation/v1/kv?automation_id={uuid.uuid4()}",
]


@pytest.mark.parametrize("route", ROUTES, ids=["automation-list", "kv"])
async def test_an_org_the_key_may_not_act_in_is_403_with_the_upstream_detail(
    users_me, route
):
    users_me.answer = httpx.Response(403, json={"detail": NOT_AUTHORIZED})

    response = await users_me.client.get(route)

    assert response.status_code == 403
    assert response.json() == {"detail": NOT_AUTHORIZED}
    assert users_me.requests[0].headers["X-Org-Id"] == str(OTHER_ORG_ID)


async def test_a_403_without_a_json_detail_is_still_403(users_me):
    users_me.answer = httpx.Response(403, text="<html>Forbidden</html>")

    response = await users_me.client.get(ROUTES[0])

    assert response.status_code == 403
    assert response.json() == {"detail": "Access denied by OpenHands API"}


async def test_a_403_is_not_cached(users_me):
    users_me.answer = httpx.Response(403, json={"detail": NOT_AUTHORIZED})
    assert (await users_me.client.get(ROUTES[0])).status_code == 403

    users_me.answer = users_me.member(OTHER_ORG_ID)
    response = await users_me.client.get(ROUTES[0])

    assert response.status_code == 200
    assert len(users_me.requests) == 2


@pytest.mark.parametrize(
    ("upstream", "detail"),
    [
        (503, "OpenHands API unavailable during authentication (HTTP 503)"),
        (500, "OpenHands API unavailable during authentication (HTTP 500)"),
        # /users/me reads only the credential and X-Org-Id, so a 404 is a
        # misconfigured openhands_api_base_url: the service's fault, not the
        # caller's.
        (404, "OpenHands API unexpected response during authentication (HTTP 404)"),
    ],
)
async def test_other_upstream_failures_stay_502_and_name_the_status(
    users_me, upstream, detail
):
    users_me.answer = httpx.Response(upstream, json={"detail": "Not Found"})

    response = await users_me.client.get(ROUTES[0])

    assert response.status_code == 502
    assert response.json() == {"detail": detail}


async def test_an_unreachable_upstream_is_502(users_me):
    users_me.answer = httpx.ConnectError("connection refused")

    response = await users_me.client.get(ROUTES[0])

    assert response.status_code == 502
    assert response.json() == {
        "detail": "Failed to reach OpenHands API for authentication"
    }
