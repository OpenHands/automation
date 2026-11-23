"""End-to-end factory outcome submission and read contracts."""

import uuid

import pytest
from sqlalchemy import select

from openhands.automation.app import app
from openhands.automation.config import clear_config_cache
from openhands.automation.kv_router import _try_resolve_user
from openhands.automation.models import (
    Automation,
    AutomationOutcomeValue,
    AutomationRun,
    AutomationRunStatus,
    Factory,
)
from openhands.automation.outcome_router import MAX_OUTCOME_ROWS_PER_RUN
from openhands.automation.utils.kv import create_kv_token


@pytest.fixture
def outcome_auth(mock_authenticated_user, monkeypatch):
    monkeypatch.setenv("AUTOMATION_KV_SECRET", "outcome-test-secret-at-least-32-bytes")
    clear_config_cache()

    # External user authentication is not available in the test database.
    async def authenticated_user():
        return mock_authenticated_user

    app.dependency_overrides[_try_resolve_user] = authenticated_user
    yield
    app.dependency_overrides.pop(_try_resolve_user, None)
    clear_config_cache()


async def make_automation(session, user, *, factory=None):
    automation = Automation(
        user_id=user.user_id,
        org_id=user.org_id,
        name="Outcomes",
        factory_id=factory.id if factory else None,
        trigger={"type": "cron", "schedule": "0 9 * * *"},
        tarball_path="s3://bucket/code.tar.gz",
        entrypoint="python3 main.py",
    )
    session.add(automation)
    await session.commit()
    return automation


async def test_factory_writes_validate_registry_and_keep_history(
    async_client, async_session, mock_authenticated_user, outcome_auth
):
    factory = Factory(
        org_id=mock_authenticated_user.org_id,
        name="Review factory",
        outcome_keys={"score": "number", "approved": "boolean"},
    )
    async_session.add(factory)
    await async_session.commit()
    automation = await make_automation(
        async_session, mock_authenticated_user, factory=factory
    )
    url = "/api/automation/v1/outcomes"
    submitted = await async_client.post(
        url,
        params={"automation_id": str(automation.id)},
        json={"values": [{"key": "score", "type": "number", "value": 42}]},
    )
    assert submitted.status_code == 201, submitted.text
    assert submitted.json() == {"stored": 1}
    for body in (
        {"payload": {"unregistered": 1}},
        {"values": [{"key": "score", "type": "string", "value": "bad"}]},
        {
            "payload": {"approved": True},
            "values": [{"key": "score", "type": "number", "value": 1}],
        },
        {
            "values": [{"key": "score", "type": "number", "value": 1}],
            "source": "platform",
        },
    ):
        response = await async_client.post(
            url, params={"automation_id": str(automation.id)}, json=body
        )
        assert response.status_code == 422, response.text

    reads = f"/api/automation/v1/factories/{factory.id}/outcomes"
    response = await async_client.get(f"{reads}/values", params={"source": "user"})
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    assert response.json()["values"][0]["source"] == "user"
    response = await async_client.get(
        f"{reads}/aggregate",
        params={
            "key": "score",
            "operation": "sum",
            "automation_id": str(automation.id),
        },
    )
    assert response.json()["value"] == 42
    assert (
        await async_client.get(f"{reads}/values", params={"source": "agent"})
    ).json()["total"] == 0

    await async_session.delete(automation)
    await async_session.commit()
    persisted = await async_session.scalar(select(AutomationOutcomeValue))
    assert persisted is not None
    assert persisted.automation_id == automation.id
    assert persisted.factory_id == factory.id

    mock_authenticated_user.org_id = uuid.uuid4()
    assert (await async_client.get(f"{reads}/values")).status_code == 404


async def test_run_token_cannot_impersonate_or_submit_without_factory(
    async_client, async_session, mock_authenticated_user, monkeypatch
):
    monkeypatch.setenv("AUTOMATION_KV_SECRET", "outcome-test-secret-at-least-32-bytes")
    clear_config_cache()

    async def no_user():
        return None

    app.dependency_overrides[_try_resolve_user] = no_user
    factory = Factory(
        org_id=mock_authenticated_user.org_id,
        name="Factory",
        outcome_keys={"score": "number"},
    )
    async_session.add(factory)
    await async_session.commit()
    automation = await make_automation(
        async_session, mock_authenticated_user, factory=factory
    )
    run = AutomationRun(automation_id=automation.id, status=AutomationRunStatus.RUNNING)
    async_session.add(run)
    await async_session.commit()
    token = create_kv_token(
        "outcome-test-secret-at-least-32-bytes", automation.id, run.id
    )
    url = "/api/automation/v1/outcomes"
    body = {"payload": {"score": 5}}
    try:
        response = await async_client.post(
            url, json=body, headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 201, response.text
        stored = await async_session.scalar(select(AutomationOutcomeValue))
        assert stored.source == "agent" and stored.run_id == run.id
        response = await async_client.post(
            url,
            json={**body, "run_id": str(uuid.uuid4())},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403
        unscoped = create_kv_token(
            "outcome-test-secret-at-least-32-bytes", automation.id
        )
        response = await async_client.post(
            url, json=body, headers={"Authorization": f"Bearer {unscoped}"}
        )
        assert response.status_code == 401
        factoryless = await make_automation(async_session, mock_authenticated_user)
        token = create_kv_token(
            "outcome-test-secret-at-least-32-bytes", factoryless.id, run.id
        )
        response = await async_client.post(
            url, json=body, headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 409
    finally:
        clear_config_cache()
        app.dependency_overrides.pop(_try_resolve_user, None)


async def test_run_row_limit_rejects_additional_submissions(
    async_client, async_session, mock_authenticated_user, outcome_auth
):
    factory = Factory(
        org_id=mock_authenticated_user.org_id,
        name="Limited factory",
        outcome_keys={"score": "number"},
    )
    async_session.add(factory)
    await async_session.commit()
    automation = await make_automation(
        async_session, mock_authenticated_user, factory=factory
    )
    run = AutomationRun(automation_id=automation.id, status=AutomationRunStatus.RUNNING)
    async_session.add(run)
    await async_session.flush()
    async_session.add_all(
        AutomationOutcomeValue(
            org_id=factory.org_id,
            factory_id=factory.id,
            automation_id=automation.id,
            run_id=run.id,
            source="agent",
            key="score",
            value_type="number",
            numeric_value=1.0,
        )
        for _ in range(MAX_OUTCOME_ROWS_PER_RUN)
    )
    await async_session.commit()

    token = create_kv_token(
        "outcome-test-secret-at-least-32-bytes", automation.id, run.id
    )
    response = await async_client.post(
        "/api/automation/v1/outcomes",
        json={"payload": {"score": 2}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422
    assert "limit" in response.json()["detail"]


async def test_nested_parser_payload_and_typed_values_share_factory_writer(
    async_client, async_session, mock_authenticated_user, outcome_auth
):
    factory = Factory(
        org_id=mock_authenticated_user.org_id,
        name="Parser factory",
        outcome_keys={"score": "number"},
    )
    async_session.add(factory)
    await async_session.commit()
    automation = await make_automation(
        async_session, mock_authenticated_user, factory=factory
    )
    automation.outcome_parsers = [
        {"key": "score", "expression": "result.score", "value_type": "number"}
    ]
    await async_session.commit()
    url = "/api/automation/v1/outcomes"
    params = {"automation_id": str(automation.id)}
    response = await async_client.post(
        url, params=params, json={"payload": {"result": {"score": 3}}}
    )
    assert response.status_code == 201, response.text
    response = await async_client.post(
        url,
        params=params,
        json={"values": [{"key": "score", "type": "number", "value": 4}]},
    )
    assert response.status_code == 201, response.text
    response = await async_client.post(
        url, params=params, json={"payload": {"result": {}}}
    )
    assert response.status_code == 422
    response = await async_client.get(
        f"/api/automation/v1/factories/{factory.id}/outcomes/aggregate",
        params={"key": "score", "operation": "sum"},
    )
    assert response.json()["value"] == 7
