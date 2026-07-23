import uuid
from typing import ClassVar

import pytest

from openhands.automation import telemetry
from openhands.automation.config import clear_config_cache
from openhands.automation.middleware import (
    TelemetryRequestContext,
    build_telemetry_request_context,
)
from openhands.automation.models import Automation, AutomationRun, AutomationRunStatus


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
        "AUTOMATION_TELEMETRY_BACKEND_ID_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    clear_config_cache()
    _MockAsyncClient.posts.clear()
    yield
    clear_config_cache()


@pytest.mark.asyncio
async def test_local_capture_uses_persistent_backend_id_and_frontend_property(
    monkeypatch, tmp_path
):
    backend_id_path = tmp_path / "backend-id"
    monkeypatch.setenv("AUTOMATION_POSTHOG_API_KEY", "ph_test")
    monkeypatch.setenv("AUTOMATION_POSTHOG_HOST", "https://posthog.example")
    monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", "http://localhost:3000")
    monkeypatch.setenv("AUTOMATION_TELEMETRY_BACKEND_ID_PATH", str(backend_id_path))
    clear_config_cache()
    monkeypatch.setattr(telemetry.httpx, "AsyncClient", _MockAsyncClient)

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
    assert properties["automation_backend_id"].startswith("automation-local:")
    assert backend_id_path.read_text().strip() == properties["automation_backend_id"]
    assert properties["frontend_distinct_id"] == "ph-fe-123"
    assert properties["client_source"] == "agent_canvas"
    assert properties["automation_id"] == str(automation.id)
    assert properties["run_id"] == str(run.id)
    assert properties["trigger_source"] == "callback"


@pytest.mark.asyncio
async def test_cloud_capture_uses_frontend_distinct_id_and_org_properties(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("AUTOMATION_POSTHOG_API_KEY", "ph_test")
    monkeypatch.setenv(
        "AUTOMATION_TELEMETRY_BACKEND_ID_PATH", str(tmp_path / "backend-id")
    )
    clear_config_cache()
    monkeypatch.setattr(telemetry.httpx, "AsyncClient", _MockAsyncClient)

    automation = _automation()

    await telemetry.capture_automation_event(
        "automation_created",
        request_context=TelemetryRequestContext(frontend_distinct_id="ph-fe-123"),
        automation=automation,
        properties={"creation_path": "prompt_preset"},
    )

    _, payload = _MockAsyncClient.posts[0]
    assert payload["distinct_id"] == "ph-fe-123"
    properties = payload["properties"]
    assert properties["deployment_mode"] == "cloud"
    assert properties["cloud_user_id"] == str(automation.user_id)
    assert properties["cloud_org_id"] == str(automation.org_id)
    assert properties["$groups"] == {"org": str(automation.org_id)}
    assert properties["creation_path"] == "prompt_preset"
    assert properties["frontend_distinct_id"] == "ph-fe-123"
    assert properties["automation_backend_id"].startswith("automation-local:")


@pytest.mark.asyncio
async def test_capture_is_disabled_without_posthog_key(monkeypatch):
    monkeypatch.setattr(telemetry.httpx, "AsyncClient", _MockAsyncClient)

    await telemetry.capture_automation_event(
        "automation_created",
        automation=_automation(),
    )

    assert _MockAsyncClient.posts == []


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
