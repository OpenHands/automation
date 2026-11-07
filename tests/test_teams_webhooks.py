"""Direct Teams callbacks through management and ingestion HTTP routes.

All principals, identifiers and tokens in this module are synthetic fixtures.
The same contract runs on SQLite and PostgreSQL, without a model or agent server.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, event, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

from openhands.automation.db import set_sqlite_mode
from openhands.automation.models import (
    Automation,
    AutomationRun,
    Base,
    CustomWebhook,
    IntegrationEvent,
)


TOKEN = base64.b64encode(b"synthetic-teams-test-token-only").decode()
ACK = {
    "type": "message",
    "text": "Request received. Any matching automation will run separately.",
}
SOURCE = "teams-test"


@pytest.fixture(params=["sqlite", "postgresql"])
async def async_engine(request, tmp_path):
    backend = request.param
    if backend == "sqlite":
        url = f"sqlite+aiosqlite:///{tmp_path / 'teams.db'}"
    else:
        container = request.getfixturevalue("postgres_container")
        url = container.get_connection_url().replace(
            "postgresql+psycopg2://", "postgresql+asyncpg://"
        )
    set_sqlite_mode(backend == "sqlite")
    engine = create_async_engine(url, hide_parameters=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()
    set_sqlite_mode(False)


def source_config(**overrides):
    return {
        "name": "Synthetic Teams source",
        "source": SOURCE,
        "signature_scheme": "teams_hmac_sha256",
        "signature_header": "Authorization",
        "webhook_secret": TOKEN,
        "acknowledgement": "teams_message",
        "event_id_expr": "join('/', [conversation.id, id])",
        "admission_expr": "channelData.tenant.id == 'tenant-test' && "
        "channelData.team.id == 'team-test' && "
        "channelData.channel.id == 'channel-test' && "
        "\"from\".aadObjectId == 'requester-test'",
        **overrides,
    }


def activity(**overrides):
    return {
        "type": "message",
        "id": "activity-test-001",
        "text": "Synthetic webhook test",
        "conversation": {"id": "conversation-test"},
        "from": {"aadObjectId": "requester-test"},
        "channelData": {
            "tenant": {"id": "tenant-test"},
            "team": {"id": "team-test"},
            "channel": {"id": "channel-test"},
        },
        **overrides,
    }


async def deliver(client, org_id, payload=None, *, token=TOKEN, raw=None, headers=None):
    body = raw if raw is not None else json.dumps(payload or activity()).encode()
    digest = hmac.new(base64.b64decode(token), body, hashlib.sha256).digest()
    return await client.post(
        f"/api/automation/v1/events/{org_id}/{SOURCE}",
        content=body,
        headers=headers
        if headers is not None
        else {"Authorization": "HMAC " + base64.b64encode(digest).decode()},
    )


async def add_automation(session, user, **trigger_overrides):
    automation = Automation(
        user_id=user.user_id,
        org_id=user.org_id,
        name="Synthetic webhook automation",
        trigger={
            "type": "event",
            "source": SOURCE,
            "on": "message",
            **trigger_overrides,
        },
        tarball_path="https://example.com/synthetic-test.tar.gz",
        entrypoint="python main.py",
    )
    session.add(automation)
    await session.commit()
    return automation


async def counts(session):
    return tuple(
        [
            await session.scalar(select(func.count()).select_from(model))
            for model in (IntegrationEvent, AutomationRun)
        ]
    )


async def test_round_trip(
    async_client, async_session, mock_authenticated_user, monkeypatch
):
    continuation = AsyncMock(
        side_effect=AssertionError("must not contact agent server")
    )
    monkeypatch.setattr(
        "openhands.automation.ingest.continue_conversation", continuation
    )
    user = mock_authenticated_user
    registered = await async_client.post(
        "/api/automation/v1/webhooks", json=source_config()
    )
    assert registered.status_code == 201, registered.text
    assert registered.json()["webhook_secret"] is None
    automation = await add_automation(async_session, user)
    response = await deliver(async_client, user.org_id)
    assert response.status_code == 200, response.text
    assert response.json() == ACK
    assert await counts(async_session) == (1, 1)
    run = (await async_session.scalars(select(AutomationRun))).one()
    assert run.automation_id == automation.id
    assert automation.user_id == user.user_id
    assert run.status.value == "PENDING"
    assert (await deliver(async_client, user.org_id)).json() == ACK
    assert await counts(async_session) == (1, 1)
    assert (
        await deliver(async_client, user.org_id, activity(id="activity-test-002"))
    ).json() == ACK
    assert await counts(async_session) == (2, 2)

    # The original ID with different content is still a duplicate. The same
    # activity ID in another conversation is distinct under this expression.
    assert (
        await deliver(
            async_client, user.org_id, activity(text="Changed synthetic content")
        )
    ).json() == ACK
    assert await counts(async_session) == (2, 2)
    assert (
        await deliver(
            async_client,
            user.org_id,
            activity(conversation={"id": "other-conversation"}),
        )
    ).json() == ACK
    assert await counts(async_session) == (3, 3)
    continuation.assert_not_awaited()


@pytest.fixture
async def registered_source(async_client):
    response = await async_client.post(
        "/api/automation/v1/webhooks", json=source_config()
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_no_match_and_committed_response(
    async_client, async_session_factory, mock_authenticated_user, registered_source
):
    response = await deliver(async_client, mock_authenticated_user.org_id)
    assert response.json() == ACK
    # Independent reader: HTTP success means the event is visible outside the
    # intake transaction even though no automation matches it.
    async with async_session_factory() as reader:
        assert await counts(reader) == (1, 0)


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": "HMAC !!!"},
        {"Authorization": "HMAC YQ=="},
        {"Authorization": "HMAC " + base64.b64encode(b"x" * 32).decode()},
    ],
)
async def test_invalid_signature(
    async_client, async_session, mock_authenticated_user, registered_source, headers
):
    response = await deliver(
        async_client, mock_authenticated_user.org_id, headers=headers
    )
    assert response.status_code == 401
    assert await counts(async_session) == (0, 0)


async def test_signed_bytes_not_reserialized(
    async_client, async_session, mock_authenticated_user, registered_source
):
    body = json.dumps(activity(), indent=2).encode()
    digest = base64.b64encode(
        hmac.new(base64.b64decode(TOKEN), body, hashlib.sha256).digest()
    ).decode()
    headers = {"Authorization": "HMAC " + digest}
    rejected = await deliver(
        async_client,
        mock_authenticated_user.org_id,
        raw=body + b" ",
        headers=headers,
    )
    assert rejected.status_code == 401
    assert await counts(async_session) == (0, 0)
    assert (
        await deliver(
            async_client, mock_authenticated_user.org_id, raw=body, headers=headers
        )
    ).json() == ACK


@pytest.mark.parametrize("raw", [b"{", b"\xff", b"[]", b"null", b'"secret-payload"'])
async def test_invalid_json_or_activity(
    async_client, async_session, mock_authenticated_user, registered_source, raw
):
    response = await deliver(async_client, mock_authenticated_user.org_id, raw=raw)
    assert response.status_code == 400
    assert "secret-payload" not in response.text
    assert await counts(async_session) == (0, 0)


@pytest.mark.parametrize("payload", [activity(type="unknown"), activity(type=None)])
async def test_unrecognized_activity(
    async_client, async_session, mock_authenticated_user, registered_source, payload
):
    response = await deliver(async_client, mock_authenticated_user.org_id, payload)
    assert response.status_code == 400
    assert await counts(async_session) == (0, 0)


@pytest.mark.parametrize("bad_id", [None, "", " ", 42, [], {}, "x" * 256])
async def test_invalid_body_id(
    async_client, async_session, mock_authenticated_user, bad_id
):
    response = await async_client.post(
        "/api/automation/v1/webhooks", json=source_config(event_id_expr="id")
    )
    assert response.status_code == 201
    response = await deliver(
        async_client, mock_authenticated_user.org_id, activity(id=bad_id)
    )
    assert response.status_code == 400
    assert await counts(async_session) == (0, 0)


async def test_missing_body_id(
    async_client, async_session, mock_authenticated_user, registered_source
):
    payload = activity()
    del payload["id"]
    response = await deliver(async_client, mock_authenticated_user.org_id, payload)
    assert response.status_code == 400
    assert await counts(async_session) == (0, 0)


@pytest.mark.parametrize("scope", ["tenant", "team", "channel", "requester", "missing"])
async def test_source_admission_before_storage_or_unfiltered_automation(
    async_client, async_session, mock_authenticated_user, registered_source, scope
):
    user = mock_authenticated_user
    await add_automation(async_session, user)
    payload = activity()
    if scope == "requester":
        payload["from"]["aadObjectId"] = "denied-requester"
    elif scope == "missing":
        del payload["channelData"]
    else:
        payload["channelData"][scope]["id"] = "denied-scope"
    response = await deliver(async_client, user.org_id, payload)
    assert response.status_code == 403
    assert await counts(async_session) == (0, 0)


@pytest.mark.parametrize(
    "override",
    [
        {"webhook_secret": None},
        {"webhook_secret": "not-base64!"},
        {"webhook_secret": ""},
        {"signature_header": "X-Signature"},
        {"admission_expr": None},
        {"admission_expr": ""},
        {"admission_expr": "broken["},
        {"event_id_expr": None},
        {"event_id_expr": "broken["},
        {"acknowledgement": None},
        {"acknowledgement": "unregistered"},
        {"event_id_header": "X-Delivery"},
    ],
)
async def test_reject_invalid_configuration(async_client, override):
    payload = source_config(**override)
    response = await async_client.post("/api/automation/v1/webhooks", json=payload)
    assert response.status_code == 422
    if payload.get("webhook_secret"):
        assert payload["webhook_secret"] not in response.text


async def test_validation_does_not_echo_secret_input(async_client):
    for secret in ("tiny", [TOKEN], {"token": TOKEN}):
        response = await async_client.post(
            "/api/automation/v1/webhooks", json=source_config(webhook_secret=secret)
        )
        assert response.status_code == 422
        assert TOKEN not in response.text
        assert "tiny" not in response.text


async def test_replace_token_and_refuse_generated_rotation(
    async_client, async_session, mock_authenticated_user, registered_source
):
    path = f"/api/automation/v1/webhooks/{registered_source['id']}"
    new_token = base64.b64encode(b"replacement-synthetic-token").decode()
    response = await async_client.patch(path, json={"webhook_secret": new_token})
    assert response.status_code == 200
    assert TOKEN not in response.text and new_token not in response.text
    assert (
        await deliver(async_client, mock_authenticated_user.org_id)
    ).status_code == 401
    assert (
        await deliver(async_client, mock_authenticated_user.org_id, token=new_token)
    ).json() == ACK
    assert (await async_client.post(path + "/rotate-secret")).status_code == 409
    assert (
        await deliver(
            async_client,
            mock_authenticated_user.org_id,
            activity(id="after-rotation"),
            token=new_token,
        )
    ).json() == ACK
    for patch in (
        {"webhook_secret": None},
        {"webhook_secret": "not-base64!"},
        {"admission_expr": None},
        {"event_id_expr": None},
        {"signature_scheme": None},
        {"acknowledgement": None},
    ):
        assert (await async_client.patch(path, json=patch)).status_code == 422
    webhook = await async_session.get(CustomWebhook, uuid.UUID(registered_source["id"]))
    assert webhook is not None
    await async_session.refresh(webhook)
    assert webhook.webhook_secret == new_token
    assert (
        await deliver(
            async_client,
            mock_authenticated_user.org_id,
            activity(id="after-rejected-patch"),
            token=new_token,
        )
    ).json() == ACK


async def test_transition_requires_new_teams_token(async_client):
    original = await async_client.post(
        "/api/automation/v1/webhooks", json={"name": "Legacy", "source": SOURCE}
    )
    assert original.status_code == 201
    path = f"/api/automation/v1/webhooks/{original.json()['id']}"
    patch = source_config()
    for key in ("name", "source", "webhook_secret"):
        patch.pop(key)
    assert (await async_client.patch(path, json=patch)).status_code == 422
    patch["webhook_secret"] = TOKEN
    assert (await async_client.patch(path, json=patch)).status_code == 200


async def test_no_conversation_call_on_deadline_source(
    async_client, async_session, mock_authenticated_user, registered_source, monkeypatch
):
    call = AsyncMock(side_effect=AssertionError("must not contact agent server"))
    monkeypatch.setattr("openhands.automation.ingest.continue_conversation", call)
    user = mock_authenticated_user
    await add_automation(async_session, user)
    # Simulate an incompatible persisted row, bypassing management validation.
    # A nonmatching `on` does not make an incompatible source safe to enable.
    await add_automation(
        async_session,
        user,
        destination="continue_conversation",
        on=["message", "other.*"],
        subject_key_expr="conversation.id",
    )
    response = await deliver(async_client, user.org_id)
    assert response.status_code == 409
    assert await counts(async_session) == (0, 0)
    call.assert_not_awaited()


async def test_incompatible_persisted_configuration_can_be_disabled_and_repaired(
    async_client, async_session, mock_authenticated_user, registered_source
):
    automation = await add_automation(
        async_session,
        mock_authenticated_user,
        destination="continue_conversation",
        subject_key_expr="conversation.id",
    )
    source_path = f"/api/automation/v1/webhooks/{registered_source['id']}"
    automation_path = f"/api/automation/v1/{automation.id}"
    assert (
        await async_client.patch(source_path, json={"enabled": False})
    ).status_code == 200
    assert (
        await async_client.patch(source_path, json={"enabled": True})
    ).status_code == 422
    assert (
        await async_client.patch(automation_path, json={"enabled": False})
    ).status_code == 200
    trigger = {
        "type": "event",
        "source": SOURCE,
        "on": "message",
        "destination": "dispatch_run",
    }
    assert (
        await async_client.patch(automation_path, json={"trigger": trigger})
    ).status_code == 200
    assert (
        await async_client.patch(source_path, json={"enabled": True})
    ).status_code == 200


async def test_profile_rejects_existing_continuation(
    async_client, async_session, mock_authenticated_user
):
    await add_automation(
        async_session,
        mock_authenticated_user,
        destination="continue_conversation",
        subject_key_expr="conversation.id",
    )
    response = await async_client.post(
        "/api/automation/v1/webhooks", json=source_config()
    )
    assert response.status_code == 422


async def test_api_rejects_continuation_create_and_update(
    async_client, async_session, mock_authenticated_user, registered_source
):
    user = mock_authenticated_user
    trigger = {
        "type": "event",
        "source": SOURCE,
        "on": ["message", "other.*"],
        "destination": "continue_conversation",
        "subject_key_expr": "conversation.id",
    }
    response = await async_client.post(
        "/api/automation/v1",
        json={
            "name": "Synthetic rejected continuation",
            "trigger": trigger,
            "tarball_path": "https://example.com/test.tar.gz",
            "entrypoint": "python main.py",
        },
    )
    assert response.status_code == 422, response.text
    automation = await add_automation(async_session, user)
    response = await async_client.patch(
        f"/api/automation/v1/{automation.id}", json={"trigger": trigger}
    )
    assert response.status_code == 422, response.text
    await async_session.refresh(automation)
    assert automation.trigger.get("destination") != "continue_conversation"


async def test_retention_is_replay_boundary(
    async_client, async_session, mock_authenticated_user, registered_source
):
    user = mock_authenticated_user
    assert (await deliver(async_client, user.org_id)).json() == ACK
    assert (await deliver(async_client, user.org_id)).json() == ACK
    assert await counts(async_session) == (1, 0)
    # Same operation as event-retention pruning; there is no timestamp check.
    await async_session.execute(delete(IntegrationEvent))
    await async_session.commit()
    assert (await deliver(async_client, user.org_id)).json() == ACK
    assert await counts(async_session) == (1, 0)


async def test_unknown_org_or_source(async_client, registered_source):
    assert (await deliver(async_client, uuid.uuid4())).status_code == 404


async def test_commit_failure_is_not_success(
    async_client, async_session, mock_authenticated_user, registered_source, monkeypatch
):
    await add_automation(async_session, mock_authenticated_user)
    capture = AsyncMock()
    monkeypatch.setattr("openhands.automation.ingest.capture_automation_event", capture)
    monkeypatch.setattr(
        async_session,
        "commit",
        AsyncMock(side_effect=SQLAlchemyError("synthetic storage failure")),
    )
    # ASGITransport propagates unhandled server exceptions by default; use the
    # same app with exception propagation disabled to inspect the HTTP result.
    from httpx import ASGITransport, AsyncClient

    from openhands.automation.app import app

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await deliver(client, mock_authenticated_user.org_id)
    assert response.status_code >= 500
    assert response.text != json.dumps(ACK)
    await async_session.rollback()
    assert await counts(async_session) == (0, 0)
    capture.assert_not_awaited()


async def test_generic_source_can_use_and_clear_body_id_and_admission(
    async_client, async_session, mock_authenticated_user
):
    config = {
        "name": "Generic synthetic source",
        "source": SOURCE,
        "webhook_secret": "generic-synthetic-secret",
        "event_id_expr": "id",
        "admission_expr": "allowed == `true`",
    }
    response = await async_client.post("/api/automation/v1/webhooks", json=config)
    assert response.status_code == 201
    path = f"/api/automation/v1/webhooks/{response.json()['id']}"

    async def send(allowed):
        raw = json.dumps(
            {"type": "message", "id": "generic-id", "allowed": allowed}
        ).encode()
        digest = hmac.new(
            config["webhook_secret"].encode(), raw, hashlib.sha256
        ).hexdigest()
        return await async_client.post(
            f"/api/automation/v1/events/{mock_authenticated_user.org_id}/{SOURCE}",
            content=raw,
            headers={"X-Signature-256": digest},
        )

    assert (await send(False)).status_code == 403
    accepted = await send(True)
    assert accepted.status_code == 200
    assert accepted.json() == {
        "received": True,
        "matched": 0,
        "runs_created": [],
        "conversations_continued": [],
    }
    assert (await send(True)).status_code == 200
    assert await counts(async_session) == (1, 0)
    # Omission retains fields; explicit null clears stored non-null values.
    assert (await async_client.patch(path, json={"name": "Renamed"})).json()[
        "event_id_expr"
    ] == "id"
    assert (
        await async_client.patch(path, json={"event_id_header": "X-Delivery"})
    ).status_code == 422
    response = await async_client.patch(
        path, json={"event_id_expr": None, "admission_expr": None}
    )
    assert response.status_code == 200
    webhook = await async_session.get(CustomWebhook, uuid.UUID(response.json()["id"]))
    assert webhook is not None
    await async_session.refresh(webhook)
    assert webhook.event_id_expr is None and webhook.admission_expr is None
    assert (await send(False)).status_code == 200
    assert await counts(async_session) == (2, 0)


async def test_source_update_cannot_enable_deadline_profile_with_continuation(
    async_client, async_session, mock_authenticated_user
):
    original = await async_client.post(
        "/api/automation/v1/webhooks", json={"name": "Legacy", "source": SOURCE}
    )
    path = f"/api/automation/v1/webhooks/{original.json()['id']}"
    await add_automation(
        async_session,
        mock_authenticated_user,
        destination="continue_conversation",
        subject_key_expr="id",
    )
    patch = source_config()
    patch.pop("source")
    assert (await async_client.patch(path, json=patch)).status_code == 422
    assert (await async_client.get(path)).json()[
        "signature_scheme"
    ] == "hmac_sha256_hex"


@pytest.mark.parametrize(
    "endpoint, extra",
    [
        ("prompt", {"prompt": "Synthetic test; must never execute"}),
        (
            "plugin",
            {
                "prompt": "Synthetic test; must never execute",
                "plugins": [{"source": "github:example/plugin"}],
            },
        ),
    ],
)
async def test_presets_reject_continuation_before_upload(
    async_client,
    async_session,
    mock_authenticated_user,
    registered_source,
    endpoint,
    extra,
    monkeypatch,
):
    from unittest.mock import Mock

    build = Mock(side_effect=AssertionError("must reject before building a tarball"))
    function = (
        "_generate_tarball" if endpoint == "prompt" else "_generate_plugin_tarball"
    )
    monkeypatch.setattr(f"openhands.automation.preset_router.{function}", build)
    response = await async_client.post(
        f"/api/automation/v1/preset/{endpoint}",
        json={
            "name": "Rejected synthetic preset",
            **extra,
            "trigger": {
                "type": "event",
                "source": SOURCE,
                "on": "message",
                "destination": "continue_conversation",
                "subject_key_expr": "conversation.id",
            },
        },
    )
    assert response.status_code == 422, response.text
    assert "Deadline-bound" in response.text
    build.assert_not_called()


async def test_draft_reports_continuation_conflict(async_client, registered_source):
    response = await async_client.post(
        "/api/automation/v1/drafts",
        json={
            "endpoint": "/v1/preset/prompt",
            "draft": {
                "name": "Rejected synthetic draft",
                "prompt": "Synthetic only",
                "trigger": {
                    "type": "event",
                    "source": SOURCE,
                    "on": "message",
                    "destination": "continue_conversation",
                    "subject_key_expr": "conversation.id",
                },
            },
        },
    )
    assert response.status_code == 201
    assert response.json()["dispatchable"] is False
    assert any(
        error["code"] == "incompatible_webhook_profile"
        for error in response.json()["validation_errors"]
    )


async def test_preflight_reports_continuation_conflict(async_client, registered_source):
    response = await async_client.post(
        "/api/automation/v1/validate",
        json={
            "endpoint": "/v1/preset/prompt",
            "draft": {
                "name": "Rejected synthetic preflight",
                "prompt": "Synthetic only",
                "trigger": {
                    "type": "event",
                    "source": SOURCE,
                    "on": "message",
                    "destination": "continue_conversation",
                    "subject_key_expr": "id",
                },
            },
        },
    )
    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert any(
        error["code"] == "incompatible_webhook_profile"
        for error in response.json()["errors"]
    )


async def test_management_reads_never_echo_teams_token(async_client, registered_source):
    for path in (
        "/api/automation/v1/webhooks",
        f"/api/automation/v1/webhooks/{registered_source['id']}",
    ):
        response = await async_client.get(path)
        assert response.status_code == 200
        assert TOKEN not in response.text


async def test_concurrent_delivery_creates_one_event_and_run(
    async_session, async_session_factory, mock_authenticated_user
):
    from openhands.automation.ingest import AcceptedEvent, accept_event

    user = mock_authenticated_user
    await add_automation(async_session, user)

    async def receive():
        async with async_session_factory() as session:
            return await accept_event(
                user.org_id,
                AcceptedEvent(
                    source=SOURCE,
                    event_key="message",
                    payload=activity(),
                    provider_event_id="concurrent-synthetic-id",
                ),
                session,
                session_factory=async_session_factory,
                allow_conversation_continuation=False,
            )

    results = await asyncio.gather(receive(), receive())
    assert sorted(result.duplicate for result in results) == [False, True]
    assert sum(len(result.run_ids) for result in results) == 1
    assert await counts(async_session) == (1, 1)


async def test_deadline_ack_precedes_telemetry_and_cold_backend_id_write(
    async_client,
    async_engine,
    async_session,
    async_session_factory,
    mock_authenticated_user,
    registered_source,
    monkeypatch,
):
    """Use real telemetry ID persistence with a slow, network-free HTTP sink.

    Observe ASGI response completion directly: ASGITransport itself waits for
    BackgroundTasks, so timing client.post() would measure the wrong boundary.
    """
    from httpx import ASGITransport, AsyncClient

    from openhands.automation import telemetry
    from openhands.automation.app import app
    from openhands.automation.config import clear_config_cache

    # Keep the regression fast if telemetry ever writes through a second SQLite
    # connection while intake still holds the writer lock.
    if async_engine.dialect.name == "sqlite":

        @event.listens_for(async_engine.sync_engine, "connect")
        def short_busy_timeout(connection, _record):
            cursor = connection.cursor()
            cursor.execute("PRAGMA busy_timeout=100")
            cursor.close()

    await add_automation(async_session, mock_authenticated_user)
    await add_automation(async_session, mock_authenticated_user)
    monkeypatch.delenv("AUTOMATION_AGENT_SERVER_URL", raising=False)
    monkeypatch.setenv("AUTOMATION_POSTHOG_API_KEY", "synthetic-metrics-key")
    clear_config_cache()
    response_sent = asyncio.Event()
    observed = []

    class Response:
        def raise_for_status(self):
            pass

    class MetricsSink:
        async def post(self, _url, *, json):
            async with async_session_factory() as reader:
                persisted = await counts(reader)
            observed.append((json["event"], response_sent.is_set(), persisted))
            await asyncio.sleep(0.01)
            return Response()

    monkeypatch.setattr(telemetry, "get_telemetry_http_client", lambda: MetricsSink())

    async def observe_response(scope, receive, send):
        async def checked_send(message):
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body"):
                response_sent.set()

        await app(scope, receive, checked_send)

    try:
        async with AsyncClient(
            transport=ASGITransport(app=observe_response), base_url="http://test"
        ) as client:
            response = await deliver(client, mock_authenticated_user.org_id)
        assert response.status_code == 200 and response.json() == ACK
        assert [item[0] for item in observed] == [
            "automation_event_matched",
            "automation_run_created",
            "automation_run_created",
        ]
        assert all(sent and persisted == (1, 2) for _, sent, persisted in observed)
        # The cold backend identity was actually written, after releasing the
        # intake transaction, rather than dropping telemetry on a lock timeout.
        assert (
            await telemetry.get_automation_backend_distinct_id(
                session_factory=async_session_factory
            )
            is not None
        )
    finally:
        clear_config_cache()


async def test_default_ingestion_telemetry_is_committed_and_detached(
    async_session,
    async_engine,
    async_session_factory,
    mock_authenticated_user,
    monkeypatch,
):
    from sqlalchemy.ext.asyncio import AsyncSession

    from openhands.automation.ingest import AcceptedEvent, accept_event

    await add_automation(async_session, mock_authenticated_user)
    observed = []

    async def capture(name, **kwargs):
        async with async_session_factory() as reader:
            assert await counts(reader) == (1, 1)
        automation = kwargs.get("automation")
        run = kwargs.get("run")
        if automation is not None:
            assert automation.user_id == mock_authenticated_user.user_id
        if run is not None:
            assert run.status.value == "PENDING"
        observed.append(name)

    monkeypatch.setattr("openhands.automation.ingest.capture_automation_event", capture)
    async with AsyncSession(async_engine, expire_on_commit=True) as session:
        result = await accept_event(
            mock_authenticated_user.org_id,
            AcceptedEvent(source=SOURCE, event_key="message", payload=activity()),
            session,
            session_factory=async_session_factory,
        )
    assert observed == ["automation_event_matched", "automation_run_created"]
    assert result.telemetry == []
