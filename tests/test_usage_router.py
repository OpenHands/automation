"""GET /v1/usage: the automation count the OpenHands server puts in its usage report."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.automation.config import clear_config_cache
from openhands.automation.models import Automation, AutomationState
from openhands.automation.utils import utcnow


URL = "/api/automation/v1/usage"
SERVICE_KEY = "test-service-key"
ORG_A = uuid.UUID("87654321-4321-8765-4321-876543218765")
ORG_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


@pytest.fixture
def service_key(monkeypatch):
    """The service key the OpenHands server shares with this service."""
    monkeypatch.setenv("AUTOMATION_SERVICE_KEY", SERVICE_KEY)
    clear_config_cache()
    yield
    clear_config_cache()


def _automation(org_id: uuid.UUID, name: str, **overrides) -> Automation:
    values = dict(
        user_id=uuid.uuid4(),
        org_id=org_id,
        name=name,
        trigger={"type": "cron", "schedule": "0 9 * * 1", "timezone": "UTC"},
        tarball_path="https://example.com/automation.tar.gz",
        entrypoint="python main.py",
    )
    values.update(overrides)
    return Automation(**values)


@pytest.mark.usefixtures("service_key")
async def test_counts_live_automations_in_every_org(
    async_client: AsyncClient, async_session: AsyncSession
):
    # Arrange
    async_session.add_all(
        [
            _automation(ORG_A, "active"),
            _automation(ORG_A, "paused", state=AutomationState.INACTIVE),
            _automation(ORG_B, "other org"),
            _automation(ORG_B, "deleted", deleted_at=utcnow()),
        ]
    )
    await async_session.commit()

    # Act
    response = await async_client.get(URL, headers={"X-Service-API-Key": SERVICE_KEY})

    # Assert
    assert response.status_code == 200, response.text
    assert response.json() == {"automations": 3}


@pytest.mark.usefixtures("service_key")
@pytest.mark.parametrize(
    "headers",
    [{}, {"X-Service-API-Key": "not-the-key"}, {"Authorization": "Bearer user-key"}],
    ids=["no-key", "wrong-key", "user-credential"],
)
async def test_rejects_a_request_without_the_service_key(
    async_client: AsyncClient, headers: dict[str, str]
):
    response = await async_client.get(URL, headers=headers)

    assert response.status_code == 401


async def test_is_unavailable_until_a_service_key_is_configured(
    async_client: AsyncClient, monkeypatch
):
    monkeypatch.setenv("AUTOMATION_SERVICE_KEY", "")
    clear_config_cache()
    try:
        response = await async_client.get(
            URL, headers={"X-Service-API-Key": SERVICE_KEY}
        )
    finally:
        clear_config_cache()

    assert response.status_code == 503
