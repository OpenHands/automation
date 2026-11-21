import importlib.metadata
import uuid
from datetime import UTC, datetime, timedelta
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
    AutomationRunAsKind,
    AutomationRunStatus,
    AutomationServiceMetadata,
    Base,
)
from openhands.automation.schemas import TelemetryConsentRequest
from openhands.automation.telemetry_router import set_telemetry_consent
from openhands.automation.utils.run import create_pending_run
from openhands.automation.utils.version import (
    SDK_PACKAGE_NAME,
    get_server_version_info,
)
from openhands.automation.utils.webhook import create_automation_run


class _Response:
    def raise_for_status(self) -> None:
        return None


class _MockAsyncClient:
    posts: ClassVar[list[tuple[str, dict]]] = []
    instances: ClassVar[list["_MockAsyncClient"]] = []

    def __init__(self, *args, **kwargs) -> None:
        self.is_closed = False
        self.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def post(self, url: str, json: dict) -> _Response:
        self.posts.append((url, json))
        return _Response()

    async def aclose(self) -> None:
        self.is_closed = True


def _automation(telemetry_distinct_id: str | None = None) -> Automation:
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
        telemetry_distinct_id=telemetry_distinct_id,
    )


def _run(automation: Automation) -> AutomationRun:
    return AutomationRun(
        id=uuid.uuid4(),
        automation_id=automation.id,
        automation=automation,
        status=AutomationRunStatus.COMPLETED,
    )


def _assert_server_version_properties(properties: dict) -> None:
    expected = get_server_version_info()
    assert properties["package_version"] == expected["package_version"]
    assert properties["sdk_version"] == expected["sdk_version"]


def test_base_properties_normalizes_sqlite_run_timestamps(monkeypatch):
    monkeypatch.setattr(
        telemetry,
        "get_server_version_info",
        lambda **kwargs: {"package_version": "test", "sdk_version": "test"},
    )
    automation = _automation()
    run = _run(automation)
    run.started_at = datetime(2026, 8, 4, 12, 0)
    run.completed_at = datetime(2026, 8, 4, 12, 0, tzinfo=UTC) + timedelta(seconds=2)

    properties = telemetry._base_properties(
        request_context=TelemetryRequestContext(),
        user=None,
        automation=automation,
        run=run,
        backend_distinct_id="automation-backend:test",
    )

    assert properties["duration_ms"] == 2000


@pytest.mark.parametrize(
    ("error_detail", "expected"),
    [
        (None, None),
        ("", None),
        ("Timed out: verification failed: boom", "verification_failed"),
        ("Timed out: sandbox stalled", "timeout"),
        ("Cancelled by user", "cancelled"),
        ("Failed to get execution context", "dispatch_error"),
        ("Script exited with code 1", "execution_error"),
    ],
)
def test_failure_kind_from_error_detail(error_detail, expected):
    assert telemetry._failure_kind_from_error_detail(error_detail) == expected


def test_base_properties_includes_derived_failure_kind(monkeypatch):
    monkeypatch.setattr(
        telemetry,
        "get_server_version_info",
        lambda **kwargs: {"package_version": "test", "sdk_version": "test"},
    )
    automation = _automation()
    run = _run(automation)
    run.status = AutomationRunStatus.FAILED
    run.error_detail = "Timed out: sandbox stalled; secret path /home/user/x"

    properties = telemetry._base_properties(
        request_context=TelemetryRequestContext(),
        user=None,
        automation=automation,
        run=run,
        backend_distinct_id="automation-backend:test",
    )

    assert properties["failure_kind"] == "timeout"
    assert "secret path" not in str(properties.values())


async def _capture_payloads(monkeypatch, captures):
    """Run captures through the real telemetry path; only PostHog HTTP is faked."""
    monkeypatch.setenv("AUTOMATION_POSTHOG_API_KEY", "ph_test")
    clear_config_cache()
    monkeypatch.setattr(telemetry.httpx, "AsyncClient", _MockAsyncClient)

    async def backend_id(**kwargs):
        return "automation-backend:test"

    monkeypatch.setattr(telemetry, "get_automation_backend_distinct_id", backend_id)
    for event, kwargs in captures:
        await telemetry.capture_automation_event(event, **kwargs)
    return [payload for _, payload in _MockAsyncClient.posts]


def _failed_run(automation, error_detail=None):
    run = _run(automation)
    run.status = AutomationRunStatus.FAILED
    run.error_detail = error_detail
    return run


@pytest.mark.parametrize(
    ("trigger_source", "expected_stage", "expected_kind"),
    [
        ("callback", "execution", "execution_error"),
        ("dispatcher", "dispatch", "dispatch_error"),
        ("watchdog", "watchdog", "execution_error"),
    ],
)
@pytest.mark.asyncio
async def test_failed_event_without_error_detail_is_categorized(
    monkeypatch, trigger_source, expected_stage, expected_kind
):
    """A terminal failure with no stored error text still gets bounded stage/kind."""
    automation = _automation()
    payloads = await _capture_payloads(
        monkeypatch,
        [
            (
                "automation_run_failed",
                {
                    "automation": automation,
                    "run": _failed_run(automation),
                    "properties": {"trigger_source": trigger_source},
                },
            )
        ],
    )

    assert len(payloads) == 1
    properties = payloads[0]["properties"]
    assert properties["failure_stage"] == expected_stage
    assert properties["failure_kind"] == expected_kind
    assert properties["trigger_source"] == trigger_source
    assert properties["run_status"] == "FAILED"
    assert properties["deployment_mode"] == "cloud"


@pytest.mark.asyncio
async def test_failed_event_keeps_explicit_failure_kind(monkeypatch):
    automation = _automation()
    payloads = await _capture_payloads(
        monkeypatch,
        [
            (
                "automation_run_failed",
                {
                    "automation": automation,
                    "run": _failed_run(automation, "Timed out: sandbox stalled"),
                    "properties": {
                        "trigger_source": "watchdog",
                        "failure_kind": "verification_failed",
                    },
                },
            )
        ],
    )

    properties = payloads[0]["properties"]
    assert properties["failure_kind"] == "verification_failed"
    assert properties["failure_stage"] == "watchdog"


@pytest.mark.asyncio
async def test_completed_event_has_no_failure_properties(monkeypatch):
    automation = _automation()
    payloads = await _capture_payloads(
        monkeypatch,
        [
            (
                "automation_run_completed",
                {
                    "automation": automation,
                    "run": _run(automation),
                    "properties": {"trigger_source": "callback"},
                },
            )
        ],
    )

    properties = payloads[0]["properties"]
    assert "failure_kind" not in properties
    assert "failure_stage" not in properties


@pytest.mark.asyncio
async def test_failed_event_never_leaks_raw_error_detail(monkeypatch):
    automation = _automation()
    payloads = await _capture_payloads(
        monkeypatch,
        [
            (
                "automation_run_failed",
                {
                    "automation": automation,
                    "run": _failed_run(
                        automation,
                        "Timed out: token=sekrit at /home/user/private",
                    ),
                    "properties": {"trigger_source": "callback"},
                },
            )
        ],
    )

    properties = payloads[0]["properties"]
    assert properties["failure_kind"] == "timeout"
    assert properties["failure_stage"] == "execution"
    assert "sekrit" not in str(properties)
    assert "private" not in str(properties)


@pytest.mark.asyncio
async def test_terminal_events_separate_success_users_from_failure_only(
    monkeypatch,
):
    """Unique users with a success are separable from failure-only users."""
    successful_user = _automation()
    failure_only_user = _automation()
    success_run = _run(successful_user)
    payloads = await _capture_payloads(
        monkeypatch,
        [
            (
                "automation_run_completed",
                {
                    "automation": successful_user,
                    "run": success_run,
                    "properties": {"trigger_source": "callback"},
                },
            ),
            (
                "automation_run_failed",
                {
                    "automation": successful_user,
                    "run": _failed_run(successful_user),
                    "properties": {"trigger_source": "watchdog"},
                },
            ),
            (
                "automation_run_failed",
                {
                    "automation": failure_only_user,
                    "run": _failed_run(failure_only_user),
                    "properties": {"trigger_source": "dispatcher"},
                },
            ),
        ],
    )

    events_by_user: dict[str, set[str]] = {}
    for payload in payloads:
        events_by_user.setdefault(payload["distinct_id"], set()).add(payload["event"])
    with_success = {
        user
        for user, events in events_by_user.items()
        if "automation_run_completed" in events
    }
    failure_only = set(events_by_user) - with_success

    assert with_success == {str(successful_user.user_id)}
    assert failure_only == {str(failure_only_user.user_id)}
    for payload in payloads:
        if payload["event"] == "automation_run_failed":
            assert payload["properties"]["failure_stage"]
            assert payload["properties"]["failure_kind"]


def test_base_properties_report_run_as_kind(monkeypatch):
    monkeypatch.setattr(
        telemetry,
        "get_server_version_info",
        lambda **kwargs: {"package_version": "test", "sdk_version": "test"},
    )
    automation = _automation()
    automation.run_as_kind = AutomationRunAsKind.SERVICE

    properties = telemetry._base_properties(
        request_context=TelemetryRequestContext(),
        user=None,
        automation=automation,
        run=None,
        backend_distinct_id="automation-backend:test",
    )

    assert properties["run_as_kind"] == "service"


@pytest.mark.asyncio
async def test_capture_contains_property_building_errors(monkeypatch):
    monkeypatch.setenv("AUTOMATION_POSTHOG_API_KEY", "ph_test")
    clear_config_cache()

    async def backend_id(**kwargs):
        return "automation-backend:test"

    monkeypatch.setattr(telemetry, "get_automation_backend_distinct_id", backend_id)

    def raise_property_error(**kwargs):
        raise TypeError("invalid timestamp arithmetic")

    monkeypatch.setattr(telemetry, "_base_properties", raise_property_error)

    await telemetry.capture_automation_event(
        "automation_run_failed",
        automation=_automation(),
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
    _MockAsyncClient.instances.clear()
    monkeypatch.setattr(telemetry, "_telemetry_http_client", None)
    yield
    clear_config_cache()


@pytest.mark.asyncio
async def test_capture_reuses_shared_http_client(monkeypatch):
    monkeypatch.setenv("AUTOMATION_POSTHOG_API_KEY", "ph_test")
    clear_config_cache()
    monkeypatch.setattr(telemetry.httpx, "AsyncClient", _MockAsyncClient)

    async def backend_id(**kwargs):
        return "automation-backend:test"

    monkeypatch.setattr(telemetry, "get_automation_backend_distinct_id", backend_id)

    await telemetry.capture_automation_event("automation_created")
    await telemetry.capture_automation_event("automation_event_received")

    assert len(_MockAsyncClient.instances) == 1
    assert len(_MockAsyncClient.posts) == 2


@pytest.mark.asyncio
async def test_close_telemetry_http_client_allows_clean_restart(monkeypatch):
    monkeypatch.setattr(telemetry.httpx, "AsyncClient", _MockAsyncClient)
    first_client = telemetry.get_telemetry_http_client()

    await telemetry.close_telemetry_http_client()

    assert first_client.is_closed is True
    second_client = telemetry.get_telemetry_http_client()
    assert second_client is not first_client


@pytest.mark.asyncio
async def test_local_capture_uses_canvas_distinct_id(monkeypatch):
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
    assert payload["distinct_id"] == "ph-fe-123"
    properties = payload["properties"]
    _assert_server_version_properties(properties)

    assert properties["automation_backend_id"] == "automation-backend:test"
    assert properties["frontend_distinct_id"] == "ph-fe-123"
    assert properties["client_source"] == "agent_canvas"
    assert properties["automation_id"] == str(automation.id)
    assert properties["run_id"] == str(run.id)
    assert properties["deployment_mode"] == "local"
    assert properties["deployment_kind"] == "local"
    assert "cloud_user_id" not in properties
    assert "cloud_org_id" not in properties
    assert "org_id" not in properties

    assert "$groups" not in properties

    assert properties["trigger_source"] == "callback"


@pytest.mark.asyncio
async def test_cloud_capture_uses_user_id_and_org_properties(monkeypatch):
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
    assert payload["distinct_id"] == str(automation.user_id)
    properties = payload["properties"]
    assert properties["deployment_mode"] == "cloud"
    assert properties["deployment_kind"] == "remote"
    assert properties["cloud_user_id"] == str(automation.user_id)
    assert properties["cloud_org_id"] == str(automation.org_id)
    assert properties["$groups"] == {"org": str(automation.org_id)}
    assert "org_id" not in properties

    assert properties["creation_path"] == "prompt_preset"
    assert "frontend_distinct_id" not in properties
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
async def test_capture_is_disabled_when_posthog_is_turned_off(monkeypatch):
    monkeypatch.setenv("AUTOMATION_POSTHOG_API_KEY", "ph_test")
    monkeypatch.setenv("AUTOMATION_POSTHOG_ENABLED", "false")
    clear_config_cache()
    monkeypatch.setattr(telemetry.httpx, "AsyncClient", _MockAsyncClient)

    async def backend_id(**kwargs):
        return "automation-backend:test"

    monkeypatch.setattr(telemetry, "get_automation_backend_distinct_id", backend_id)

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

    automation = _automation()
    await telemetry.capture_automation_event(
        "automation_created",
        automation=automation,
    )

    assert len(_MockAsyncClient.posts) == 1
    _, payload = _MockAsyncClient.posts[0]
    assert payload["distinct_id"] == str(automation.user_id)
    assert payload["properties"]["deployment_mode"] == "cloud"
    assert payload["properties"]["deployment_kind"] == "remote"


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

        automation = _automation(telemetry_distinct_id="ph-fe-consented")
        run = _run(automation)
        run.telemetry_distinct_id = "ph-fe-consented"

        await telemetry.capture_automation_event(
            "automation_run_created",
            automation=automation,
            run=run,
            session_factory=session_factory,
        )

        assert len(_MockAsyncClient.posts) == 1
        _, payload = _MockAsyncClient.posts[0]
        assert payload["event"] == "automation_run_created"
        assert payload["distinct_id"] == "ph-fe-consented"
        assert payload["properties"]["deployment_mode"] == "local"
        assert payload["properties"]["deployment_kind"] == "local"
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
            assert not await telemetry.get_stored_telemetry_consent(
                session=session,
                frontend_distinct_id="ph-fe-a",
            )
            assert await telemetry.get_stored_telemetry_consent(
                session=session,
                frontend_distinct_id="ph-fe-b",
            )

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
async def test_background_runs_inherit_automation_telemetry_identity():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            automation = _automation(telemetry_distinct_id="ph-fe-owner")
            session.add(automation)
            await session.flush()

            scheduled_run = await create_pending_run(session, automation)
            webhook_run = await create_automation_run(automation, session)

            assert scheduled_run.telemetry_distinct_id == "ph-fe-owner"
            assert webhook_run.telemetry_distinct_id == "ph-fe-owner"
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
            permissions=["view_automations", "manage_automations"],
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
        assert payload["distinct_id"] == "ph-fe-link"
        assert properties["frontend_distinct_id"] == "ph-fe-link"
        assert properties["client_source"] == "agent_canvas"
        assert properties["client_version"] == "1.2.3"
        assert properties["deployment_kind"] == "local"
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
            permissions=["view_automations", "manage_automations"],
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


@pytest.mark.asyncio
async def test_local_capture_falls_back_to_backend_id_without_canvas_actor(
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

    await telemetry.capture_automation_event("automation_event_matched")

    _, payload = _MockAsyncClient.posts[0]
    assert payload["distinct_id"] == "automation-backend:local-api"


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


def test_server_version_properties_cache_package_metadata_lookup(monkeypatch):
    calls = 0

    def package_version(name: str) -> str:
        nonlocal calls
        calls += 1
        assert name == SDK_PACKAGE_NAME
        return "1.2.3"

    monkeypatch.setattr(importlib.metadata, "version", package_version)
    telemetry._installed_sdk_version.cache_clear()
    try:
        assert telemetry._server_version_properties()["sdk_version"] == "1.2.3"
        assert telemetry._server_version_properties()["sdk_version"] == "1.2.3"
        assert calls == 1
    finally:
        telemetry._installed_sdk_version.cache_clear()


def test_server_version_properties_do_not_cache_missing_sdk(monkeypatch):
    def missing_package_version(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", missing_package_version)
    telemetry._installed_sdk_version.cache_clear()
    try:
        assert telemetry._server_version_properties()["sdk_version"] == "unknown"

        monkeypatch.setattr(importlib.metadata, "version", lambda name: "1.2.3")
        assert telemetry._server_version_properties()["sdk_version"] == "1.2.3"
    finally:
        telemetry._installed_sdk_version.cache_clear()


@pytest.mark.asyncio
async def test_self_hosted_override_labels_cloud_mode_events_as_local(
    monkeypatch, request
):
    request.addfinalizer(clear_config_cache)
    monkeypatch.setenv("AUTOMATION_POSTHOG_API_KEY", "ph_test")
    monkeypatch.setenv("AUTOMATION_TELEMETRY_DEPLOYMENT_MODE", "self_hosted")
    clear_config_cache()
    monkeypatch.setattr(telemetry.httpx, "AsyncClient", _MockAsyncClient)

    async def backend_id(**kwargs):
        return "automation-backend:ohe"

    monkeypatch.setattr(telemetry, "get_automation_backend_distinct_id", backend_id)
    automation = _automation()

    await telemetry.capture_automation_event(
        "automation_created", automation=automation
    )

    _, payload = _MockAsyncClient.posts[-1]
    properties = payload["properties"]
    assert properties["deployment_mode"] == "self_hosted"
    assert properties["deployment_kind"] == "local"
    assert properties["cloud_user_id"] == str(automation.user_id)
