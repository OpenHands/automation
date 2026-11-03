"""GitLab as a built-in event source, forwarded by the OpenHands server."""

import hashlib
import hmac
import json
import uuid

import jmespath
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from openhands.automation.auth import AuthenticatedUser
from openhands.automation.config import clear_config_cache
from openhands.automation.event_schemas import parse_event
from openhands.automation.event_schemas.gitlab import parse_gitlab_event
from openhands.automation.models import Automation, AutomationRun, CustomWebhook


SHARED_SECRET = "test-secret"
LEGACY_SECRET = "legacy-custom-secret"


@pytest.fixture
def org_id(mock_authenticated_user: AuthenticatedUser) -> uuid.UUID:
    return mock_authenticated_user.org_id


@pytest.fixture(autouse=True)
def clear_settings_cache(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AUTOMATION_WEBHOOK_SECRET", SHARED_SECRET)
    clear_config_cache()
    yield
    clear_config_cache()


def merge_request_payload(action: str = "open") -> dict:
    return {
        "object_kind": "merge_request",
        "event_type": "merge_request",
        "user": {"id": 7, "username": "alice"},
        "project": {"id": 42, "path_with_namespace": "acme/api"},
        "object_attributes": {
            "iid": 3,
            "action": action,
            "target_branch": "main",
            "title": "feat: add endpoint",
        },
    }


def forwarded(payload: dict, org_id: uuid.UUID) -> dict:
    """The envelope the OpenHands server forwards."""
    return {
        "organization": {"git_org": "acme", "openhands_org_id": str(org_id)},
        "payload": payload,
    }


def sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def add_automation(session, user, org_id, on: str, filter: str | None = None):
    trigger = {"type": "event", "source": "gitlab", "on": on}
    if filter:
        trigger["filter"] = filter
    session.add(
        Automation(
            id=uuid.uuid4(),
            user_id=user.user_id,
            org_id=org_id,
            name="GitLab automation",
            tarball_path="oh-internal://uploads/test.tar.gz",
            entrypoint="python main.py",
            trigger=trigger,
        )
    )
    await session.commit()


async def add_legacy_custom_webhook(session, org_id, *, enabled: bool = True):
    """A custom webhook registered as `gitlab` before the source was built in."""
    session.add(
        CustomWebhook(
            org_id=org_id,
            name="GitLab",
            source="gitlab",
            webhook_secret=LEGACY_SECRET,
            event_key_expr="object_kind",
            signature_header="X-Signature-256",
            enabled=enabled,
        )
    )
    await session.commit()


async def run_count(session) -> int:
    return len((await session.execute(select(AutomationRun))).scalars().all())


class TestParseGitLabEvent:
    @pytest.mark.parametrize(
        ("payload", "event_key"),
        [
            (merge_request_payload("open"), "merge_request.open"),
            (merge_request_payload("merge"), "merge_request.merge"),
            (
                {"object_kind": "issue", "object_attributes": {"action": "close"}},
                "issue.close",
            ),
            (
                {"object_kind": "pipeline", "object_attributes": {"status": "failed"}},
                "pipeline.failed",
            ),
            ({"object_kind": "build", "build_status": "success"}, "build.success"),
            (
                {"object_kind": "note", "object_attributes": {"id": 1}},
                "note",
            ),
        ],
    )
    def test_event_key_is_kind_and_action(self, payload, event_key):
        event = parse_event("gitlab", payload)
        assert event.source == "gitlab"
        assert event.event_key == event_key
        assert event.payload == payload

    def test_a_kind_without_its_action_keys_on_the_kind(self):
        assert parse_gitlab_event({"object_kind": "merge_request"}).event_key == (
            "merge_request"
        )

    @pytest.mark.parametrize(
        "payload",
        [
            {"object_kind": "push", "user_username": "alice"},
            {"object_kind": None},
            {"event_name": "project_create"},
        ],
    )
    def test_kinds_these_webhooks_never_send_are_rejected(self, payload):
        with pytest.raises(ValueError):
            parse_gitlab_event(payload)


@pytest.mark.asyncio
async def test_requested_types_let_the_forwarder_detect_each_kind(
    async_client: AsyncClient,
):
    response = await async_client.get(
        "/api/automation/v1/events/gitlab/requested-types",
        headers={"X-Hub-Signature-256": sign(b"gitlab", SHARED_SECRET)},
    )

    assert response.status_code == 200
    rules = response.json()["event_detection_rules"]
    assert [rule["event_type"] for rule in rules] == [
        "build",
        "issue",
        "merge_request",
        "note",
        "pipeline",
    ]
    detected = [
        rule["event_type"]
        for rule in rules
        if jmespath.search(rule["jmespath"], merge_request_payload())
    ]
    assert detected == ["merge_request"]


@pytest.mark.asyncio
async def test_forwarded_merge_request_runs_a_matching_automation(
    async_client: AsyncClient,
    async_session,
    org_id: uuid.UUID,
    mock_authenticated_user,
):
    await add_automation(
        async_session,
        mock_authenticated_user,
        org_id,
        on="merge_request.open",
        filter="object_attributes.target_branch == 'main'",
    )
    body = json.dumps(forwarded(merge_request_payload(), org_id)).encode()

    response = await async_client.post(
        f"/api/automation/v1/events/{org_id}/gitlab",
        content=body,
        headers={
            "X-Hub-Signature-256": sign(body, SHARED_SECRET),
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 200
    assert response.json()["matched"] == 1
    assert len(response.json()["runs_created"]) == 1


@pytest.mark.asyncio
async def test_forwarded_event_with_another_action_does_not_match(
    async_client: AsyncClient,
    async_session,
    org_id: uuid.UUID,
    mock_authenticated_user,
):
    await add_automation(
        async_session, mock_authenticated_user, org_id, on="merge_request.open"
    )
    body = json.dumps(forwarded(merge_request_payload("close"), org_id)).encode()

    response = await async_client.post(
        f"/api/automation/v1/events/{org_id}/gitlab",
        content=body,
        headers={
            "X-Hub-Signature-256": sign(body, SHARED_SECRET),
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 200
    assert response.json()["matched"] == 0


class TestLegacyCustomGitLabWebhook:
    """An org's own `gitlab` custom webhook keeps working and does not double-fire."""

    @pytest.mark.asyncio
    async def test_its_direct_deliveries_still_run_automations(
        self, async_client, async_session, org_id, mock_authenticated_user
    ):
        await add_legacy_custom_webhook(async_session, org_id)
        await add_automation(
            async_session, mock_authenticated_user, org_id, on="merge_request"
        )
        body = json.dumps(merge_request_payload()).encode()

        response = await async_client.post(
            f"/api/automation/v1/events/{org_id}/gitlab",
            content=body,
            headers={
                "X-Signature-256": sign(body, LEGACY_SECRET),
                "Content-Type": "application/json",
            },
        )

        assert response.status_code == 200
        assert response.json()["matched"] == 1

    @pytest.mark.asyncio
    async def test_forwarded_copies_are_acknowledged_without_running(
        self, async_client, async_session, org_id, mock_authenticated_user
    ):
        await add_legacy_custom_webhook(async_session, org_id)
        await add_automation(async_session, mock_authenticated_user, org_id, on="*")
        body = json.dumps(forwarded(merge_request_payload(), org_id)).encode()

        response = await async_client.post(
            f"/api/automation/v1/events/{org_id}/gitlab",
            content=body,
            headers={
                "X-Hub-Signature-256": sign(body, SHARED_SECRET),
                "Content-Type": "application/json",
            },
        )

        assert response.status_code == 200
        assert response.json()["matched"] == 0
        assert await run_count(async_session) == 0

    @pytest.mark.asyncio
    async def test_an_unsigned_delivery_is_still_rejected(
        self, async_client, async_session, org_id
    ):
        await add_legacy_custom_webhook(async_session, org_id)
        body = json.dumps(merge_request_payload()).encode()

        response = await async_client.post(
            f"/api/automation/v1/events/{org_id}/gitlab",
            content=body,
            headers={
                "X-Signature-256": sign(body, "wrong-secret"),
                "X-Hub-Signature-256": sign(body, "wrong-secret"),
                "Content-Type": "application/json",
            },
        )

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_once_disabled_the_built_in_source_takes_over(
        self, async_client, async_session, org_id, mock_authenticated_user
    ):
        await add_legacy_custom_webhook(async_session, org_id, enabled=False)
        await add_automation(
            async_session, mock_authenticated_user, org_id, on="merge_request.*"
        )
        body = json.dumps(forwarded(merge_request_payload(), org_id)).encode()

        response = await async_client.post(
            f"/api/automation/v1/events/{org_id}/gitlab",
            content=body,
            headers={
                "X-Hub-Signature-256": sign(body, SHARED_SECRET),
                "Content-Type": "application/json",
            },
        )

        assert response.status_code == 200
        assert response.json()["matched"] == 1
