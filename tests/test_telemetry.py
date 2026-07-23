import uuid
from typing import ClassVar

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from openhands.automation import telemetry
from openhands.automation.auth import AuthenticatedUser, AuthMethod
from openhands.automation.config import clear_config_cache
from openhands.automation.middleware import (
    TelemetryRequestContext,
    build_telemetry_request_context,
)
from openhands.automation.models import (
    Automation,
    AutomationRun,
    AutomationRunStatus,
    AutomationServiceMetadata,
    Base,
)
from openhands.automation.schemas import TelemetryConsentRequest
from openhands.automation.telemetry_router import set_telemetry_consent


class _Response:
    def raise_for_status(self) -> None:
        return None


class _MockAsyncClient:
    posts: ClassVar[list[tuple[str, dict]]] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def post(self, url: str, json: dict) -> _Response:
        self.posts.append((url, json))
        return _Response()


def _automation() -> Automation:
    return Automation(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        name="Daily report",
        trigger={"type": "cron", "schedule": "0 9 * * *"},
        tarball_path="https://example.com/a.tar",
        entrypoint="python main.py",
        enabled=True,
        timeout=300,
    )


def _run(automation: Automation) -> AutomationRun:
    return AutomationRun(
        id=uuid.uuid4(),
        automation_id=automation.id,
        automation=automation,
        status=AutomationRunStatus.COMPLETED,
    )


@pytest.fixture(autouse=True)
def _reset_config(monkeypatch):
    for name in (
        "AUTOMATION_POSTHOG_API_KEY",
        "AUTOMATION_POSTHOG_HOST",
        "AUTOMATION_AGENT_SERVER_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    clear_config_cache()
    _MockAsyncClient.posts.clear()
    yield
    clear_config_cache()


@pytest.mark.asyncio
async def test_local_capture_uses_backend_id_and_frontend_property(monkeypatch):
    monkeypatch.setenv("AUTOMATION_POSTHOG_API_KEY", "ph_test")
    monkeypatch.setenv("AUTOMATION_POSTHOG_HOST", "https://posthog.example")
    monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", "http://localhost:3000")
    clear_config_cache()
    monkeypatch.setattr(telemetry.httpx, "AsyncClient", _MockAsyncClient)

    async def backend_id(**kwargs):
        return "automation-backend:test"

    monkeypatch.setattr(telemetry, "get_automation_backend_distinct_id", backend_id)

    async def stored_consent(**kwargs):
        return True

    monkeypatch.setattr(telemetry, "get_stored_telemetry_consent", stored_consent)

    automation = _automation()
    run = _run(automation)
    context = TelemetryRequestContext(
        frontend_distinct_id="ph-fe-123",
        client_source="agent_canvas",
        client_version="1.2.3",
    )

    await telemetry.capture_automation_event(
        "automation_run_completed",
        request_context=context,
        automation=automation,
        run=run,
        properties={"trigger_source": "callback"},
    )

    assert len(_MockAsyncClient.posts) == 1
    url, payload = _MockAsyncClient.posts[0]
    assert url == "https://posthog.example/capture/"
    assert payload["event"] == "automation_run_completed"
    assert payload["distinct_id"] == "automation-backend:test"
    properties = payload["properties"]
    assert properties["automation_backend_id"] == "automation-backend:test"
    assert properties["frontend_distinct_id"] == "ph-fe-123"
    assert properties["client_source"] == "agent_canvas"
    assert properties["automation_id"] == str(automation.id)
    assert properties["run_id"] == str(run.id)
    assert properties["deployment_mode"] == "local"
    assert "cloud_user_id" not in properties
    assert "cloud_org_id" not in properties
    assert "org_id" not in properties

    assert "$groups" not in properties

    assert properties["trigger_source"] == "callback"


@pytest.mark.asyncio
async def test_cloud_capture_uses_backend_id_and_org_properties(monkeypatch):
    monkeypatch.setenv("AUTOMATION_POSTHOG_API_KEY", "ph_test")
    clear_config_cache()
    monkeypatch.setattr(telemetry.httpx, "AsyncClient", _MockAsyncClient)

    async def backend_id(**kwargs):
        return "automation-backend:cloud"

    monkeypatch.setattr(telemetry, "get_automation_backend_distinct_id", backend_id)

    automation = _automation()

    await telemetry.capture_automation_event(
        "automation_created",
        request_context=TelemetryRequestContext(frontend_distinct_id="ph-fe-123"),
        automation=automation,
        properties={"creation_path": "prompt_preset"},
    )

    _, payload = _MockAsyncClient.posts[0]
    assert payload["distinct_id"] == "automation-backend:cloud"
    properties = payload["properties"]
    assert properties["deployment_mode"] == "cloud"
    assert properties["cloud_user_id"] == str(automation.user_id)
    assert properties["cloud_org_id"] == str(automation.org_id)
    assert properties["$groups"] == {"org": str(automation.org_id)}
    assert "org_id" not in properties

    assert properties["creation_path"] == "prompt_preset"
    assert properties["frontend_distinct_id"] == "ph-fe-123"
    assert properties["automation_backend_id"] == "automation-backend:cloud"


@pytest.mark.asyncio
async def test_capture_is_disabled_without_posthog_key(monkeypatch):
    monkeypatch.setattr(telemetry.httpx, "AsyncClient", _MockAsyncClient)

    await telemetry.capture_automation_event(
        "automation_created",
        automation=_automation(),
    )

    assert _MockAsyncClient.posts == []


@pytest.mark.asyncio
async def test_local_capture_requires_stored_telemetry_consent(monkeypatch):
    monkeypatch.setenv("AUTOMATION_POSTHOG_API_KEY", "ph_test")
    monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", "http://localhost:3000")
    clear_config_cache()
    monkeypatch.setattr(telemetry.httpx, "AsyncClient", _MockAsyncClient)

    async def backend_id(**kwargs):
        return "automation-backend:local"

    monkeypatch.setattr(telemetry, "get_automation_backend_distinct_id", backend_id)

    await telemetry.capture_automation_event(
        "automation_created",
        automation=_automation(),
        request_context=TelemetryRequestContext(client_source="agent_canvas"),
    )

    assert _MockAsyncClient.posts == []


@pytest.mark.asyncio
async def test_cloud_capture_does_not_require_frontend_distinct_id(monkeypatch):
    monkeypatch.setenv("AUTOMATION_POSTHOG_API_KEY", "ph_test")
    clear_config_cache()
    monkeypatch.setattr(telemetry.httpx, "AsyncClient", _MockAsyncClient)

    async def backend_id(**kwargs):
        return "automation-backend:cloud"

    monkeypatch.setattr(telemetry, "get_automation_backend_distinct_id", backend_id)

    await telemetry.capture_automation_event(
        "automation_created",
        automation=_automation(),
    )

    assert len(_MockAsyncClient.posts) == 1
    _, payload = _MockAsyncClient.posts[0]
    assert payload["distinct_id"] == "automation-backend:cloud"
    assert payload["properties"]["deployment_mode"] == "cloud"


@pytest.mark.asyncio
async def test_local_capture_uses_stored_consent_without_request_id(monkeypatch):
    monkeypatch.setenv("AUTOMATION_POSTHOG_API_KEY", "ph_test")
    monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", "http://localhost:3000")
    clear_config_cache()
    monkeypatch.setattr(telemetry.httpx, "AsyncClient", _MockAsyncClient)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            await telemetry.set_stored_telemetry_consent(
                session,
                consent_granted=True,
                frontend_distinct_id="ph-fe-consented",
            )
            await session.commit()

        await telemetry.capture_automation_event(
            "automation_run_dispatched",
            automation=_automation(),
            session_factory=session_factory,
        )

        assert len(_MockAsyncClient.posts) == 1
        _, payload = _MockAsyncClient.posts[0]
        assert payload["event"] == "automation_run_dispatched"
        assert payload["properties"]["deployment_mode"] == "local"
        assert "frontend_distinct_id" not in payload["properties"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_stored_telemetry_consent_tracks_any_granted_frontend_id():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            assert not await telemetry.set_stored_telemetry_consent(
                session,
                consent_granted=False,
                frontend_distinct_id="ph-fe-a",
            )
            assert await telemetry.set_stored_telemetry_consent(
                session,
                consent_granted=True,
                frontend_distinct_id="ph-fe-b",
            )
            assert await telemetry.get_stored_telemetry_consent(session=session)

            assert await telemetry.set_stored_telemetry_consent(
                session,
                consent_granted=False,
                frontend_distinct_id="ph-fe-a",
            )
            assert (
                await telemetry.set_stored_telemetry_consent(
                    session,
                    consent_granted=False,
                    frontend_distinct_id="ph-fe-b",
                )
                is False
            )
            assert not await telemetry.get_stored_telemetry_consent(session=session)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_telemetry_consent_route_stores_consent_and_emits_link_event(
    monkeypatch,
):
    monkeypatch.setenv("AUTOMATION_POSTHOG_API_KEY", "ph_test")
    monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", "http://localhost:3000")
    clear_config_cache()
    monkeypatch.setattr(telemetry.httpx, "AsyncClient", _MockAsyncClient)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        request = _request(
            "/api/automation/v1/telemetry/consent",
            endpoint_name="set_telemetry_consent",
        )
        request.state.telemetry_context = TelemetryRequestContext(
            client_source="agent_canvas",
            client_version="1.2.3",
        )
        user = AuthenticatedUser(
            user_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            email="local@example.com",
            role="admin",
            permissions=["manage_automations"],
            auth_method=AuthMethod.LOCAL_API_KEY,
        )

        async with session_factory() as session:
            response = await set_telemetry_consent(
                TelemetryConsentRequest(
                    consent_granted=True,
                    frontend_distinct_id="ph-fe-link",
                ),
                request,
                user,
                session,
            )

        assert response.consent_granted is True
        assert len(_MockAsyncClient.posts) == 1
        _, payload = _MockAsyncClient.posts[0]
        properties = payload["properties"]
        assert payload["event"] == "automation_telemetry_consent_granted"
        assert payload["distinct_id"].startswith("automation-backend:")
        assert properties["frontend_distinct_id"] == "ph-fe-link"
        assert properties["client_source"] == "agent_canvas"
        assert properties["client_version"] == "1.2.3"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_telemetry_consent_route_rejects_cloud_mode(monkeypatch):
    monkeypatch.delenv("AUTOMATION_AGENT_SERVER_URL", raising=False)
    monkeypatch.setenv("AUTOMATION_POSTHOG_API_KEY", "ph_test")
    clear_config_cache()

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        request = _request(
            "/api/automation/v1/telemetry/consent",
            endpoint_name="set_telemetry_consent",
        )
        user = AuthenticatedUser(
            user_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            email="cloud@example.com",
            role="admin",
            permissions=["manage_automations"],
            auth_method=AuthMethod.API_KEY,
        )

        async with session_factory() as session:
            with pytest.raises(HTTPException) as exc_info:
                await set_telemetry_consent(
                    TelemetryConsentRequest(
                        consent_granted=True,
                        frontend_distinct_id="ph-fe-cloud",
                    ),
                    request,
                    user,
                    session,
                )

            assert exc_info.value.status_code == 400
            assert "cloud mode" in str(exc_info.value.detail)
            stored = await session.scalar(
                select(AutomationServiceMetadata.value).where(
                    AutomationServiceMetadata.key
                    == telemetry.TELEMETRY_CONSENT_METADATA_KEY
                )
            )
            assert stored is None
            assert _MockAsyncClient.posts == []
    finally:
        await engine.dispose()


def test_build_telemetry_request_context_extracts_canvas_headers():
    scope = {
        "headers": [
            (b"x-openhands-telemetry-distinct-id", b" ph-fe-123 "),
            (b"x-openhands-client", b"agent_canvas"),
            (b"x-openhands-client-version", b"1.2.3"),
        ]
    }

    context = build_telemetry_request_context(scope)

    assert context == TelemetryRequestContext(
        frontend_distinct_id="ph-fe-123",
        client_source="agent_canvas",
        client_version="1.2.3",
    )


class _Route:
    path = "/api/automation/v1/{automation_id}"


def _request(path: str, *, endpoint_name: str = "list_automations"):
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    def endpoint():
        return None

    endpoint.__name__ = endpoint_name
    return telemetry.Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [],
            "query_string": b"",
            "server": ("testserver", 80),
            "scheme": "http",
            "client": ("testclient", 50000),
            "endpoint": endpoint,
            "route": _Route(),
        },
        receive,
    )


def test_should_capture_api_route_for_v1_and_public_paths():
    assert telemetry.should_capture_api_route(_request("/api/automation/v1"))
    assert telemetry.should_capture_api_route(_request("/sdk-version"))
    assert telemetry.should_capture_api_route(_request("/api/automation/server_info"))
    assert not telemetry.should_capture_api_route(_request("/docs"))
    assert not telemetry.should_capture_api_route(_request("/automations"))


@pytest.mark.asyncio
async def test_capture_api_route_event_uses_endpoint_name_and_route_template(
    monkeypatch,
):
    monkeypatch.setenv("AUTOMATION_POSTHOG_API_KEY", "ph_test")
    clear_config_cache()
    monkeypatch.setattr(telemetry.httpx, "AsyncClient", _MockAsyncClient)

    async def backend_id(**kwargs):
        return "automation-backend:api"

    monkeypatch.setattr(telemetry, "get_automation_backend_distinct_id", backend_id)

    request = _request("/api/automation/v1/123", endpoint_name="get_automation")
    user = AuthenticatedUser(
        user_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        email="user@example.com",
        role="admin",
        permissions=["manage_automations"],
        auth_method=AuthMethod.API_KEY,
    )
    request.state.authenticated_user = user

    await telemetry.capture_api_route_event(
        request,
        status_code=200,
        duration_ms=12,
    )

    _, payload = _MockAsyncClient.posts[0]
    assert payload["event"] == "automation_api_get_automation"
    properties = payload["properties"]
    assert properties["http_method"] == "GET"
    assert properties["route_path"] == "/api/automation/v1/{automation_id}"
    assert properties["route_operation"] == "get_automation"
    assert properties["status_code"] == 200
    assert properties["deployment_mode"] == "cloud"
    assert properties["cloud_user_id"] == str(user.user_id)
    assert properties["cloud_org_id"] == str(user.org_id)
    assert properties["$groups"] == {"org": str(user.org_id)}
    assert "org_id" not in properties
    assert properties["success"] is True
    assert properties["duration_ms"] == 12


@pytest.mark.asyncio
async def test_capture_api_route_event_in_local_mode_omits_cloud_identity(
    monkeypatch,
):
    monkeypatch.setenv("AUTOMATION_POSTHOG_API_KEY", "ph_test")
    monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", "http://localhost:3000")
    clear_config_cache()
    monkeypatch.setattr(telemetry.httpx, "AsyncClient", _MockAsyncClient)

    async def backend_id(**kwargs):
        return "automation-backend:local-api"

    monkeypatch.setattr(telemetry, "get_automation_backend_distinct_id", backend_id)

    async def stored_consent(**kwargs):
        return True

    monkeypatch.setattr(telemetry, "get_stored_telemetry_consent", stored_consent)

    request = _request("/api/automation/v1/123", endpoint_name="get_automation")
    request.state.telemetry_context = TelemetryRequestContext(
        frontend_distinct_id="ph-fe-local"
    )
    request.state.authenticated_user = AuthenticatedUser(
        user_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        email="local@example.com",
        role="admin",
        permissions=["manage_automations"],
        auth_method=AuthMethod.LOCAL_API_KEY,
    )

    await telemetry.capture_api_route_event(
        request,
        status_code=200,
        duration_ms=12,
    )

    _, payload = _MockAsyncClient.posts[0]
    properties = payload["properties"]
    assert payload["distinct_id"] == "automation-backend:local-api"
    assert properties["deployment_mode"] == "local"
    assert properties["automation_backend_id"] == "automation-backend:local-api"
    assert "cloud_user_id" not in properties
    assert "cloud_org_id" not in properties
    assert "org_id" not in properties
    assert "$groups" not in properties

    assert properties["success"] is True
    assert properties["duration_ms"] == 12


@pytest.mark.asyncio
async def test_backend_distinct_id_is_db_backed_and_stable():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        first = await telemetry.get_automation_backend_distinct_id(
            session_factory=session_factory
        )
        second = await telemetry.get_automation_backend_distinct_id(
            session_factory=session_factory
        )

        assert first is not None
        assert first.startswith("automation-backend:")
        assert second == first

        async with session_factory() as session:
            row_count = await session.scalar(
                select(func.count()).select_from(AutomationServiceMetadata)
            )
            stored = await session.scalar(
                select(AutomationServiceMetadata.value).where(
                    AutomationServiceMetadata.key
                    == telemetry.TELEMETRY_BACKEND_DISTINCT_ID_KEY
                )
            )

        assert row_count == 1
        assert stored == first
    finally:
        await engine.dispose()
