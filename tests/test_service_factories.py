"""Enterprise's service-key routes into factories: the lookup it uses to confirm
a factory exists, and the worklog purge this service sends after a delete."""

import logging
import uuid
from collections.abc import AsyncGenerator

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.app import app
from openhands.automation.config import clear_config_cache
from openhands.automation.models import Automation, Factory


BASE = "/api/automation/v1"
LOOKUP = f"{BASE}/service/factories"
SERVICE_KEY = "test-service-key"
ORG_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")
OTHER_ORG_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
ENTERPRISE_URL = "https://enterprise.test"


@pytest.fixture
def service_key(monkeypatch):
    monkeypatch.setenv("AUTOMATION_SERVICE_KEY", SERVICE_KEY)
    clear_config_cache()
    yield
    clear_config_cache()


async def _factory(session: AsyncSession, org_id: uuid.UUID, slug: str) -> Factory:
    factory = Factory(org_id=org_id, name=slug, slug=slug)
    session.add(factory)
    await session.commit()
    return factory


def _params(org_id: uuid.UUID, ids: list[uuid.UUID]) -> httpx.QueryParams:
    return httpx.QueryParams([("org_id", str(org_id)), *(("id", str(i)) for i in ids)])


# --- Lookup ---


@pytest.mark.usefixtures("service_key")
async def test_lookup_returns_only_requested_ids_that_exist_in_the_org(
    async_client: AsyncClient, async_session: AsyncSession
):
    mine = await _factory(async_session, ORG_ID, "mine")
    also_mine = await _factory(async_session, ORG_ID, "also-mine")
    unrequested = await _factory(async_session, ORG_ID, "unrequested")
    theirs = await _factory(async_session, OTHER_ORG_ID, "theirs")
    missing = uuid.uuid4()

    response = await async_client.get(
        LOOKUP,
        params=_params(ORG_ID, [mine.id, also_mine.id, theirs.id, missing]),
        headers={"X-Service-API-Key": SERVICE_KEY},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"factories"}
    assert {(f["id"], f["org_id"]) for f in body["factories"]} == {
        (str(mine.id), str(ORG_ID)),
        (str(also_mine.id), str(ORG_ID)),
    }
    assert str(unrequested.id) not in response.text


@pytest.mark.usefixtures("service_key")
async def test_lookup_of_only_unknown_ids_is_an_empty_list(async_client: AsyncClient):
    response = await async_client.get(
        LOOKUP,
        params=_params(ORG_ID, [uuid.uuid4()]),
        headers={"X-Service-API-Key": SERVICE_KEY},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"factories": []}


@pytest.mark.usefixtures("service_key")
async def test_lookup_accepts_100_ids_and_refuses_101(async_client: AsyncClient):
    headers = {"X-Service-API-Key": SERVICE_KEY}

    hundred = await async_client.get(
        LOOKUP,
        params=_params(ORG_ID, [uuid.uuid4() for _ in range(100)]),
        headers=headers,
    )
    too_many = await async_client.get(
        LOOKUP,
        params=_params(ORG_ID, [uuid.uuid4() for _ in range(101)]),
        headers=headers,
    )

    assert hundred.status_code == 200, hundred.text
    assert too_many.status_code == 422, too_many.text


@pytest.mark.usefixtures("service_key")
@pytest.mark.parametrize(
    "params",
    [
        pytest.param([("org_id", str(ORG_ID))], id="no-ids"),
        pytest.param([("id", str(uuid.uuid4()))], id="no-org"),
        pytest.param([("org_id", "x"), ("id", str(uuid.uuid4()))], id="bad-org"),
        pytest.param([("org_id", str(ORG_ID)), ("id", "x")], id="bad-id"),
    ],
)
async def test_lookup_rejects_malformed_requests(async_client: AsyncClient, params):
    response = await async_client.get(
        LOOKUP, params=params, headers={"X-Service-API-Key": SERVICE_KEY}
    )

    assert response.status_code == 422, response.text


@pytest.mark.usefixtures("service_key")
@pytest.mark.parametrize(
    "headers",
    [{}, {"X-Service-API-Key": "wrong"}, {"Authorization": f"Bearer {SERVICE_KEY}"}],
    ids=["missing", "wrong", "bearer-is-not-the-header"],
)
async def test_lookup_is_401_without_the_service_key(
    async_client: AsyncClient, headers
):
    response = await async_client.get(
        LOOKUP, params=_params(ORG_ID, [uuid.uuid4()]), headers=headers
    )

    assert response.status_code == 401, response.text


async def test_lookup_is_503_when_no_service_key_is_configured(
    async_client: AsyncClient, monkeypatch
):
    monkeypatch.delenv("AUTOMATION_SERVICE_KEY", raising=False)
    clear_config_cache()

    response = await async_client.get(
        LOOKUP,
        params=_params(ORG_ID, [uuid.uuid4()]),
        headers={"X-Service-API-Key": SERVICE_KEY},
    )

    assert response.status_code == 503, response.text
    clear_config_cache()


# --- Delete notification ---


class EnterpriseWorklogApi:
    """Enterprise's worklog purge route. ``status`` is what it answers; ``error``
    makes it unreachable. ``seen`` records each request with whether the factory
    row was already gone from the database when it arrived."""

    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory
        self.status = 204
        self.error: Exception | None = None
        self.seen: list[tuple[httpx.Request, bool]] = []

    async def respond(self, request: httpx.Request) -> httpx.Response:
        factory_id = uuid.UUID(request.url.path.split("/")[-2])
        async with self.session_factory() as session:
            committed = await session.get(Factory, factory_id) is None
        self.seen.append((request, committed))
        if self.error is not None:
            raise self.error
        return httpx.Response(self.status)


@pytest.fixture
async def enterprise(
    async_client, async_session_factory, monkeypatch
) -> AsyncGenerator[EnterpriseWorklogApi, None]:
    """Cloud mode with a service key, and Enterprise serving the purge route."""
    monkeypatch.delenv("AUTOMATION_AGENT_SERVER_URL", raising=False)
    monkeypatch.setenv("AUTOMATION_SERVICE_KEY", SERVICE_KEY)
    monkeypatch.setenv("AUTOMATION_OPENHANDS_API_BASE_URL", ENTERPRISE_URL)
    clear_config_cache()
    api = EnterpriseWorklogApi(async_session_factory)
    await app.state.http_client.aclose()
    app.state.http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(api.respond)
    )
    yield api
    clear_config_cache()


async def _delete_empty_factory(
    client: AsyncClient, session: AsyncSession
) -> tuple[Factory, httpx.Response]:
    factory = await _factory(session, ORG_ID, "doomed")
    return factory, await client.delete(f"{BASE}/factories/doomed")


async def test_delete_sends_the_purge_after_the_delete_commits(
    async_client, async_session, enterprise
):
    factory, response = await _delete_empty_factory(async_client, async_session)

    assert response.status_code == 204, response.text
    assert len(enterprise.seen) == 1
    request, committed = enterprise.seen[0]
    assert request.method == "DELETE"
    assert str(request.url) == (
        f"{ENTERPRISE_URL}/api/service/orgs/{ORG_ID}/factories/{factory.id}/worklogs"
    )
    assert request.headers["X-Service-API-Key"] == SERVICE_KEY
    assert committed, "the purge was sent before the delete committed"


@pytest.mark.parametrize("status", [404, 500, 503])
async def test_an_enterprise_error_does_not_undo_the_delete(
    async_client, async_session, enterprise, caplog, status
):
    enterprise.status = status
    caplog.set_level(logging.WARNING)

    factory, response = await _delete_empty_factory(async_client, async_session)

    assert response.status_code == 204, response.text
    assert (await async_client.get(f"{BASE}/factories/doomed")).status_code == 404
    assert f"returned HTTP {status}" in caplog.text
    assert str(factory.id) in caplog.text


async def test_an_unreachable_enterprise_does_not_undo_the_delete(
    async_client, async_session, enterprise, caplog
):
    enterprise.error = httpx.ConnectError("refused")
    caplog.set_level(logging.WARNING)

    factory, response = await _delete_empty_factory(async_client, async_session)

    assert response.status_code == 204, response.text
    assert (await async_client.get(f"{BASE}/factories/doomed")).status_code == 404
    assert f"Worklog purge for deleted factory {factory.id} failed" in caplog.text


async def test_nothing_is_sent_in_local_mode(
    async_client, async_session, enterprise, monkeypatch
):
    monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", "http://localhost:3000")
    clear_config_cache()

    _, response = await _delete_empty_factory(async_client, async_session)

    assert response.status_code == 204, response.text
    assert enterprise.seen == []


async def test_nothing_is_sent_without_a_service_key(
    async_client, async_session, enterprise, monkeypatch, caplog
):
    monkeypatch.delenv("AUTOMATION_SERVICE_KEY")
    clear_config_cache()
    caplog.set_level(logging.WARNING)

    _, response = await _delete_empty_factory(async_client, async_session)

    assert response.status_code == 204, response.text
    assert enterprise.seen == []
    assert "no service key" in caplog.text


async def test_a_refused_delete_sends_nothing(async_client, async_session, enterprise):
    factory = await _factory(async_session, ORG_ID, "busy")
    async_session.add(
        Automation(
            user_id=uuid.uuid4(),
            org_id=ORG_ID,
            name="Member",
            trigger={"type": "cron", "schedule": "0 9 * * 1", "timezone": "UTC"},
            tarball_path="https://example.com/a.tar.gz",
            entrypoint="python main.py",
            factory_id=factory.id,
        )
    )
    await async_session.commit()

    response = await async_client.delete(f"{BASE}/factories/busy")

    assert response.status_code == 409, response.text
    assert enterprise.seen == []
