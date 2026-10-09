"""Tests for the capabilities and preflight validation endpoints."""

import asyncio
import dataclasses
import json
import uuid

import httpx
import pytest

from openhands.automation import capabilities_router
from openhands.automation.app import app
from openhands.automation.auth import AuthMethod, authenticate_request
from openhands.automation.config import clear_config_cache
from openhands.automation.models import CustomWebhook
from openhands.sdk.workspace import RemoteWorkspace, repo as sdk_repo


# Test UUID matching mock_authenticated_user fixture
TEST_ORG_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")

CAPABILITIES_URL = "/api/automation/v1/capabilities"
VALIDATE_URL = "/api/automation/v1/validate"

CRON_DRAFT = {
    "name": "PR reviewer",
    "prompt": "Review open pull requests.",
    "repos": [
        {"url": "OpenHands/agent-server-gui", "ref": "main", "provider": "github"}
    ],
    "trigger": {"type": "cron", "schedule": "*/15 * * * *", "timezone": "UTC"},
}

EVENT_DRAFT = {
    "name": "Mention responder",
    "prompt": "Reply to the comment that mentioned us.",
    "trigger": {
        "type": "event",
        "source": "github",
        "on": "issue_comment.created",
        "filter": "icontains(comment.body, '@openhands')",
    },
}

BUNDLE_DRAFT = {
    "name": "PR reviewer",
    "tarball_path": "oh-internal://uploads/12345678-1234-1234-1234-123456789abc",
    "entrypoint": "uv run main.py",
    "trigger": {"type": "cron", "schedule": "*/15 * * * *", "timezone": "UTC"},
}

GITHUB_USER = {"id": 3, "login": "someone"}


def with_trigger(draft: dict, **overrides: str) -> dict:
    """Copy a draft with individual trigger fields replaced."""
    return {**draft, "trigger": {**draft["trigger"], **overrides}}


def preflight(draft: dict, **extra: object) -> dict:
    """Build a preflight request body, defaulting to the prompt-preset endpoint."""
    return {
        "automationId": "github-pr-reviewer",
        "endpoint": "/v1/preset/prompt",
        "draft": draft,
        **extra,
    }


def integration_requirement(
    integration_id: str,
    *,
    transport: str,
    locator: str,
    auth_strategy: str = "none",
    secret_names: list[str] | None = None,
) -> dict:
    """Build one integration requirement in the public setup contract."""
    alternative: dict[str, object] = {
        "transport": transport,
        "locator": locator,
        "authStrategy": auth_strategy,
    }
    if secret_names:
        alternative["secretNames"] = secret_names
    return {"id": integration_id, "alternatives": [alternative]}


async def install_outbound_transport(handler) -> None:
    """Replace the app's dependency client with a deterministic mock transport."""
    await app.state.http_client.aclose()
    app.state.http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
    )


def comment_event(body: str) -> dict:
    """A GitHub issue_comment payload carrying the given comment text."""
    return {
        "action": "created",
        "issue": {"number": 7, "title": "A bug", "state": "open", "user": GITHUB_USER},
        "comment": {"id": 1, "body": body, "user": GITHUB_USER},
        "repository": {
            "id": 2,
            "name": "agent-server-gui",
            "full_name": "OpenHands/agent-server-gui",
            "private": False,
        },
        "sender": GITHUB_USER,
    }


def addressed_errors(body: dict) -> list[tuple[str | None, str]]:
    """The field and code of every reported error, in order."""
    return [(error["field"], error["code"]) for error in body["errors"]]


@pytest.fixture(autouse=True)
def reset_config_cache():
    """Configuration is cached, so each test reads it fresh and leaves it clean."""
    clear_config_cache()
    yield
    clear_config_cache()


@pytest.fixture
def ready_deployment(monkeypatch):
    """A deployment that can mint the API key every run needs.

    Nothing is advertised at all without it, so a test asserting on what a
    deployment offers has to say so rather than inherit it from the environment.
    """
    monkeypatch.setenv("AUTOMATION_SERVICE_KEY", "service-key")
    clear_config_cache()


@pytest.fixture
def configured_deployment(ready_deployment, monkeypatch):
    """A deployment that receives webhooks, mints API keys, and stores state."""
    monkeypatch.setenv("AUTOMATION_WEBHOOK_SECRET", "webhook-secret")
    monkeypatch.setenv("AUTOMATION_KV_SECRET", "kv-secret")
    clear_config_cache()


class TestGetCapabilities:
    """Tests for GET /v1/capabilities endpoint."""

    async def test_agent_profiles_are_offered_without_an_agent_server(
        self, async_client, ready_deployment, monkeypatch
    ):
        """In cloud mode the OpenHands app server resolves the profile."""
        monkeypatch.delenv("AUTOMATION_AGENT_SERVER_URL", raising=False)
        clear_config_cache()

        response = await async_client.get(CAPABILITIES_URL)

        assert "agentProfiles" in response.json()["features"]

    async def test_configured_deployment_advertises_event_support(
        self, async_client, configured_deployment
    ):
        """A deployment that can receive webhooks offers both trigger kinds."""
        response = await async_client.get(CAPABILITIES_URL)

        assert response.status_code == 200
        body = response.json()
        assert body["ready"] is True
        assert body["triggerKinds"] == ["cron", "event"]
        assert body["eventSources"] == ["bitbucket_data_center", "github", "jira_dc"]
        assert body["eventTypes"] == [
            "issue_comment.*",
            "issues.*",
            "pull_request.*",
            "pull_request_review.*",
            "push",
            "release.*",
        ]
        assert body["triggers"]["event"]["filterLanguage"] == "jmespath"
        assert "icontains" in body["triggers"]["event"]["filterFunctions"]
        assert "UTC" in body["triggers"]["cron"]["timezones"]
        assert "webhookDelivery" in body["features"]
        assert "kvStore" in body["features"]
        assert "customTarball" in body["features"]

    async def test_advertises_the_configured_timeout_ceiling(
        self, async_client, ready_deployment, monkeypatch
    ):
        """The setup client learns the same maximum the API enforces."""
        monkeypatch.setenv("AUTOMATION_MAX_RUN_DURATION", "900")
        clear_config_cache()

        response = await async_client.get(CAPABILITIES_URL)

        assert response.status_code == 200
        assert response.json()["maxAutomationTimeoutSeconds"] == 900

    async def test_deployment_without_webhook_secret_withdraws_event_support(
        self, async_client, ready_deployment, monkeypatch
    ):
        """No webhook secret means no event can arrive, so none is offered."""
        monkeypatch.setenv("AUTOMATION_WEBHOOK_SECRET", "")
        clear_config_cache()

        response = await async_client.get(CAPABILITIES_URL)

        body = response.json()
        assert body["triggerKinds"] == ["cron"]
        assert body["eventSources"] == []
        assert body["eventTypes"] == []
        assert "event" not in body["triggers"]
        assert "webhookDelivery" not in body["features"]

    async def test_only_enabled_custom_sources_are_advertised(
        self, async_client, async_session, ready_deployment, monkeypatch
    ):
        """An organization's own webhooks make event triggers available again."""
        monkeypatch.setenv("AUTOMATION_WEBHOOK_SECRET", "")
        clear_config_cache()
        async_session.add_all(
            [
                CustomWebhook(
                    org_id=TEST_ORG_ID,
                    name="Linear",
                    source="linear",
                    webhook_secret="linear-secret",
                ),
                CustomWebhook(
                    org_id=TEST_ORG_ID,
                    name="Retired",
                    source="retired",
                    webhook_secret="retired-secret",
                    enabled=False,
                ),
            ]
        )
        await async_session.commit()

        response = await async_client.get(CAPABILITIES_URL)

        body = response.json()
        assert body["eventSources"] == ["linear"]
        assert body["triggerKinds"] == ["cron", "event"]

    async def test_cloud_deployment_without_service_key_is_not_ready(
        self, async_client, monkeypatch
    ):
        """Runs cannot execute without the key that mints their credentials."""
        monkeypatch.setenv("AUTOMATION_SERVICE_KEY", "")
        monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", "")
        clear_config_cache()

        response = await async_client.get(CAPABILITIES_URL)

        body = response.json()
        assert body["ready"] is False
        assert body["triggerKinds"] == []
        assert body["features"] == []
        assert body["triggers"] == {}

    async def test_advertised_cron_floor_follows_the_scheduler_interval(
        self, async_client, ready_deployment, monkeypatch
    ):
        """A slower scheduler raises the shortest interval it can honour."""
        monkeypatch.setenv("AUTOMATION_SCHEDULER_INTERVAL_SECONDS", "300")
        clear_config_cache()

        response = await async_client.get(CAPABILITIES_URL)

        assert response.json()["triggers"]["cron"]["minIntervalSeconds"] == 300


class TestValidateDraft:
    """Tests for POST /v1/validate endpoint."""

    async def test_valid_draft_reports_no_errors(self, async_client):
        """A draft the service would accept passes preflight."""
        response = await async_client.post(VALIDATE_URL, json=preflight(CRON_DRAFT))

        assert response.status_code == 200
        assert response.json() == {
            "valid": True,
            "errors": [],
            "sampleEventMatched": None,
        }

    @pytest.mark.parametrize(
        ("draft", "expected_error"),
        [
            pytest.param(
                {key: value for key, value in CRON_DRAFT.items() if key != "name"},
                ("name", "missing"),
                id="missing-required-field",
            ),
            pytest.param(
                {**CRON_DRAFT, "unexpected": "value"},
                ("unexpected", "extra_forbidden"),
                id="unknown-field",
            ),
            pytest.param(
                with_trigger(CRON_DRAFT, schedule="0 0 31 2 *"),
                ("trigger.schedule", "value_error"),
                id="field-inside-the-trigger-union",
            ),
            pytest.param(
                {**CRON_DRAFT, "repos": [{"url": "OpenHands/agent-server-gui"}]},
                ("repos[0]", "value_error"),
                id="item-inside-a-list",
            ),
        ],
    )
    async def test_schema_violations_are_addressed_to_their_field(
        self, async_client, draft, expected_error
    ):
        """An invalid draft is a 200 naming the path the caller can highlight."""
        response = await async_client.post(VALIDATE_URL, json=preflight(draft))

        assert response.status_code == 200
        body = response.json()
        assert body["valid"] is False
        assert addressed_errors(body) == [expected_error]

    async def test_schedule_faster_than_the_deployment_floor_is_rejected(
        self, async_client
    ):
        """Only the deployment knows how often it can actually fire an automation."""
        draft = with_trigger(CRON_DRAFT, schedule="*/10 * * * * *")

        response = await async_client.post(VALIDATE_URL, json=preflight(draft))

        body = response.json()
        assert body["valid"] is False
        assert addressed_errors(body) == [("trigger.schedule", "interval_too_short")]

    @pytest.mark.parametrize(
        ("event_pattern", "expected_errors"),
        [
            pytest.param(
                "pull_request_review_comment.created",
                [("trigger.on", "event_type_not_delivered")],
                id="type-the-deployment-cannot-parse",
            ),
            pytest.param("pull_request.*", [], id="wildcard-over-supported-types"),
        ],
    )
    async def test_event_types_are_checked_against_what_the_deployment_parses(
        self, async_client, configured_deployment, event_pattern, expected_errors
    ):
        """An event that cannot be parsed would never fire the automation."""
        draft = with_trigger(EVENT_DRAFT, on=event_pattern)

        response = await async_client.post(VALIDATE_URL, json=preflight(draft))

        body = response.json()
        assert addressed_errors(body) == expected_errors
        assert body["valid"] == (expected_errors == [])

    async def test_event_trigger_for_an_undeliverable_source_is_rejected(
        self, async_client, monkeypatch
    ):
        """Without a webhook configuration the trigger could never fire."""
        monkeypatch.setenv("AUTOMATION_WEBHOOK_SECRET", "")
        clear_config_cache()

        response = await async_client.post(VALIDATE_URL, json=preflight(EVENT_DRAFT))

        body = response.json()
        assert body["valid"] is False
        assert addressed_errors(body) == [
            ("trigger.source", "event_source_not_configured")
        ]

    @pytest.mark.parametrize(
        ("comment_body", "expected_match"),
        [
            pytest.param("please look at this @openhands", True, id="matches-filter"),
            pytest.param("nothing to see here", False, id="fails-filter"),
        ],
    )
    async def test_sample_event_reports_whether_it_would_fire_the_trigger(
        self, async_client, configured_deployment, comment_body, expected_match
    ):
        """A real payload answers the question the filter expression asks."""
        response = await async_client.post(
            VALIDATE_URL,
            json=preflight(EVENT_DRAFT, sampleEvent=comment_event(comment_body)),
        )

        body = response.json()
        assert body["valid"] is True
        assert body["sampleEventMatched"] is expected_match

    async def test_unparseable_sample_event_is_reported_as_an_error(
        self, async_client, configured_deployment
    ):
        """A payload the service cannot recognise answers nothing."""
        response = await async_client.post(
            VALIDATE_URL,
            json=preflight(EVENT_DRAFT, sampleEvent={"unrecognised": True}),
        )

        body = response.json()
        assert body["valid"] is False
        assert addressed_errors(body) == [("sampleEvent", "unparseable_sample_event")]
        assert body["sampleEventMatched"] is None

    async def test_unknown_model_profile_is_reported_against_the_model_field(
        self, async_client, mock_authenticated_user
    ):
        """A profile the user cannot use is caught before an automation exists."""
        user = dataclasses.replace(
            mock_authenticated_user, model_profile_names=frozenset({"fast"})
        )
        app.dependency_overrides[authenticate_request] = lambda: user

        response = await async_client.post(
            VALIDATE_URL, json=preflight({**CRON_DRAFT, "model": "missing-profile"})
        )

        body = response.json()
        assert body["valid"] is False
        assert addressed_errors(body) == [("model", "model_profile_not_found")]

    @pytest.mark.parametrize("requirements", [None, {"integrations": []}])
    async def test_valid_bundle_draft_reports_no_errors(
        self, async_client, requirements
    ):
        """A catalog entry shipping its own tarball can preflight its draft too."""
        response = await async_client.post(
            VALIDATE_URL,
            json=preflight(BUNDLE_DRAFT, endpoint="/v1", requirements=requirements),
        )

        assert response.status_code == 200
        assert response.json()["valid"] is True

    async def test_bundle_draft_checks_integration_requirements(self, async_client):
        def outbound(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/v1/settings"
            return httpx.Response(200, json={"agent_settings": {"mcp_config": {}}})

        await install_outbound_transport(outbound)
        response = await async_client.post(
            VALIDATE_URL,
            json=preflight(
                BUNDLE_DRAFT,
                endpoint="/v1",
                requirements={
                    "integrations": [
                        integration_requirement(
                            "postgres", transport="stdio", locator="postgres"
                        )
                    ]
                },
            ),
        )

        assert response.status_code == 200
        assert addressed_errors(response.json()) == [
            (None, "integration_not_configured")
        ]

    async def test_validation_requires_view_permission(
        self, async_client, mock_authenticated_user
    ):
        user = dataclasses.replace(mock_authenticated_user, permissions=[])
        app.dependency_overrides[authenticate_request] = lambda: user

        def outbound(request: httpx.Request) -> httpx.Response:
            raise AssertionError("Unauthorized validation must not probe dependencies")

        await install_outbound_transport(outbound)
        response = await async_client.post(
            VALIDATE_URL,
            json=preflight(CRON_DRAFT, requirements={"integrations": []}),
        )

        assert response.status_code == 403

    @pytest.mark.parametrize(
        ("auth_fields", "auth_strategy"),
        [
            ({"auth": "legacy-token"}, "bearer"),
            ({"auth": "oauth"}, "oauth2"),
            ({"api_key": "legacy-token"}, "api_key"),
            ({"headers": {"Authorization": "Bearer legacy-token"}}, "bearer"),
        ],
    )
    async def test_legacy_mcp_auth_does_not_block_preflight(
        self, async_client, auth_fields, auth_strategy
    ):
        mcp_url = "https://mcp.example.test/legacy"
        probes = []

        def outbound(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/settings":
                return httpx.Response(
                    200,
                    json={
                        "agent_settings": {
                            "mcp_config": {"legacy": {"url": mcp_url, **auth_fields}}
                        }
                    },
                )
            assert request.url.path == "/api/v1/settings/mcp/legacy/test"
            probes.append(request)
            assert "legacy-token" not in request.content.decode()
            return httpx.Response(200, json={"ok": True})

        await install_outbound_transport(outbound)
        response = await async_client.post(
            VALIDATE_URL,
            json=preflight(
                {**CRON_DRAFT, "repos": None},
                requirements={
                    "integrations": [
                        integration_requirement(
                            "legacy",
                            transport="shttp",
                            locator=mcp_url,
                            auth_strategy=auth_strategy,
                        )
                    ]
                },
            ),
        )

        assert response.status_code == 200
        assert response.json()["valid"] is True
        assert len(probes) == 1
        assert "legacy-token" not in response.text

    @pytest.mark.parametrize("include_usable", [True, False])
    @pytest.mark.parametrize(
        "invalid_fields",
        [
            {"auth": ["invalid-secret"]},
            {"transport": []},
            {"enabled": "true"},
        ],
    )
    async def test_invalid_stored_mcp_entry_is_isolated(
        self, async_client, include_usable, invalid_fields
    ):
        mcp_url = "https://mcp.example.test/usable"
        config = {"broken": {"url": mcp_url, **invalid_fields}}
        if include_usable:
            config["usable"] = {"url": mcp_url}

        def outbound(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/settings":
                return httpx.Response(
                    200, json={"agent_settings": {"mcp_config": config}}
                )
            assert include_usable
            assert request.url.path == "/api/v1/settings/mcp/usable/test"
            return httpx.Response(200, json={"ok": True})

        await install_outbound_transport(outbound)
        response = await async_client.post(
            VALIDATE_URL,
            json=preflight(
                {**CRON_DRAFT, "repos": None},
                requirements={
                    "integrations": [
                        integration_requirement(
                            "usable", transport="shttp", locator=mcp_url
                        )
                    ]
                },
            ),
        )

        assert response.status_code == 200
        assert response.json()["valid"] is include_usable
        assert addressed_errors(response.json()) == (
            [] if include_usable else [(None, "integration_not_configured")]
        )
        assert "invalid-secret" not in response.text

    async def test_bundle_draft_reports_schema_errors(self, async_client):
        """A body the raw create endpoint would 422 is caught before creation."""
        draft = {**BUNDLE_DRAFT}
        del draft["entrypoint"]

        response = await async_client.post(
            VALIDATE_URL, json=preflight(draft, endpoint="/v1")
        )

        body = response.json()
        assert body["valid"] is False
        assert ("entrypoint", "missing") in addressed_errors(body)

    async def test_bundle_draft_is_checked_against_the_cron_floor(self, async_client):
        """Trigger checks are model-agnostic, so a bundle gets them unchanged."""
        response = await async_client.post(
            VALIDATE_URL,
            json=preflight(
                with_trigger(BUNDLE_DRAFT, schedule="*/10 * * * * *"), endpoint="/v1"
            ),
        )

        body = response.json()
        assert body["valid"] is False
        assert addressed_errors(body) == [("trigger.schedule", "interval_too_short")]

    async def test_accepts_a_profile_of_the_callers_organization(
        self, async_client, agent_profiles_api
    ):
        """A setup form with a profile selected gets past preflight."""
        draft = {**BUNDLE_DRAFT, "agent_profile_id": agent_profiles_api.profile_id}

        response = await async_client.post(
            VALIDATE_URL,
            json=preflight(draft, endpoint="/v1"),
            headers=agent_profiles_api.caller_auth,
        )

        assert response.json()["valid"] is True

    async def test_a_profile_already_seen_is_not_looked_up_again(
        self, async_client, agent_profiles_api
    ):
        """A form validates on every edit, so a known selection costs one lookup."""
        draft = {**BUNDLE_DRAFT, "agent_profile_id": agent_profiles_api.profile_id}
        body = preflight(draft, endpoint="/v1")

        responses = [
            await async_client.post(
                VALIDATE_URL, json=body, headers=agent_profiles_api.caller_auth
            )
            for _ in range(3)
        ]

        assert all(response.json()["valid"] for response in responses)
        assert len(agent_profiles_api.requests) == 1

    async def test_reports_a_profile_the_organization_does_not_have(
        self, async_client, agent_profiles_api
    ):
        draft = {**BUNDLE_DRAFT, "agent_profile_id": str(uuid.uuid4())}

        response = await async_client.post(
            VALIDATE_URL,
            json=preflight(draft, endpoint="/v1"),
            headers=agent_profiles_api.caller_auth,
        )

        body = response.json()
        assert addressed_errors(body) == [("agent_profile_id", "invalid_agent_profile")]

    async def test_opted_in_profile_dependency_failure_fails_closed(
        self, async_client, agent_profiles_api
    ):
        """An opted-in deployment preflight fails closed on profile outage."""
        agent_profiles_api.response = httpx.Response(500)
        draft = {
            **with_trigger(BUNDLE_DRAFT, schedule="*/10 * * * * *"),
            "agent_profile_id": agent_profiles_api.profile_id,
        }

        response = await async_client.post(
            VALIDATE_URL,
            json=preflight(draft, endpoint="/v1", requirements={"integrations": []}),
            headers=agent_profiles_api.caller_auth,
        )

        assert response.status_code == 503
        assert response.json() == {
            "detail": "Preflight validation is temporarily unavailable."
        }

    async def test_legacy_profile_dependency_failure_preserves_other_verdicts(
        self, async_client, agent_profiles_api
    ):
        """Legacy clients keep the main-branch advisory profile behavior."""
        agent_profiles_api.response = httpx.Response(500)
        draft = {
            **with_trigger(BUNDLE_DRAFT, schedule="*/10 * * * * *"),
            "agent_profile_id": agent_profiles_api.profile_id,
        }

        response = await async_client.post(
            VALIDATE_URL,
            json=preflight(draft, endpoint="/v1"),
            headers=agent_profiles_api.caller_auth,
        )

        assert response.status_code == 200
        assert addressed_errors(response.json()) == [
            ("trigger.schedule", "interval_too_short")
        ]

    @pytest.mark.parametrize(
        ("requirements", "expected_status"),
        [(None, 200), ({"integrations": []}, 503)],
    )
    async def test_profile_request_timeout_respects_preflight_opt_in(
        self, async_client, agent_profiles_api, requirements, expected_status
    ):
        """A profile transport timeout is advisory only for legacy clients."""
        agent_profiles_api.raise_timeout = True
        draft = {
            **with_trigger(BUNDLE_DRAFT, schedule="*/10 * * * * *"),
            "agent_profile_id": agent_profiles_api.profile_id,
        }

        response = await async_client.post(
            VALIDATE_URL,
            json=preflight(draft, endpoint="/v1", requirements=requirements),
            headers=agent_profiles_api.caller_auth,
        )

        assert response.status_code == expected_status
        if requirements is None:
            assert addressed_errors(response.json()) == [
                ("trigger.schedule", "interval_too_short")
            ]
        else:
            assert response.json() == {
                "detail": "Preflight validation is temporarily unavailable."
            }

    async def test_profile_lookup_obeys_the_total_preflight_timeout(
        self, async_client, monkeypatch
    ):
        async def stalled_lookup(*_args):
            await asyncio.sleep(1)

        monkeypatch.setattr(
            capabilities_router, "ensure_agent_profile_exists", stalled_lookup
        )
        monkeypatch.setattr(
            capabilities_router, "_PREFLIGHT_TOTAL_TIMEOUT_SECONDS", 0.01
        )
        draft = {**BUNDLE_DRAFT, "agent_profile_id": str(uuid.uuid4())}

        response = await async_client.post(
            VALIDATE_URL, json=preflight(draft, endpoint="/v1")
        )

        assert response.status_code == 503
        assert response.json() == {
            "detail": "Preflight validation is temporarily unavailable."
        }

    async def test_unknown_creation_endpoint_is_rejected(self, async_client):
        """Preflight only validates drafts for the endpoints it may name."""
        response = await async_client.post(
            VALIDATE_URL, json={"endpoint": "/v1/anything", "draft": CRON_DRAFT}
        )

        assert response.status_code == 422

    async def test_required_secret_and_integration_failures_are_step_addressable(
        self, async_client
    ):
        """Every unsatisfied prerequisite points back to the setup step."""
        slack_url = "https://mcp.example.test/slack"

        def outbound(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/secrets/search":
                return httpx.Response(200, json={"items": [], "next_page_id": None})
            if request.url.path == "/api/v1/settings":
                return httpx.Response(
                    200,
                    json={
                        "agent_settings": {
                            "mcp_config": {
                                "slack": {
                                    "transport": "http",
                                    "url": slack_url,
                                    "auth": {
                                        "strategy": "api_key",
                                        "value": "**********",
                                    },
                                }
                            }
                        }
                    },
                )
            raise AssertionError(f"Unexpected outbound request: {request.url}")

        await install_outbound_transport(outbound)
        draft = {**CRON_DRAFT, "repos": None}
        response = await async_client.post(
            VALIDATE_URL,
            json=preflight(
                draft,
                requirements={
                    "integrations": [
                        integration_requirement(
                            "slack",
                            transport="shttp",
                            locator=slack_url,
                            auth_strategy="api_key",
                            secret_names=["SLACK_BOT_TOKEN"],
                        ),
                        integration_requirement(
                            "postgres",
                            transport="stdio",
                            locator="postgres",
                        ),
                    ]
                },
            ),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["valid"] is False
        assert [(error["code"], error["step"]) for error in body["errors"]] == [
            ("credential_missing", "prerequisites"),
            ("integration_not_configured", "prerequisites"),
        ]
        assert "SLACK_BOT_TOKEN" in body["errors"][0]["message"]

    async def test_cloud_preflight_probes_matching_integration_without_secret_leakage(
        self, async_client
    ):
        """Cloud probes the stored server while only returning a coarse verdict."""
        mcp_url = "https://mcp.example.test/github"
        sentinel = "provider-secret-should-never-leak"
        seen_probe = False

        def outbound(request: httpx.Request) -> httpx.Response:
            nonlocal seen_probe
            if request.url.path == "/api/v1/secrets/search":
                return httpx.Response(
                    200,
                    json={
                        "items": [{"name": "GITHUB_TOKEN", "description": sentinel}],
                        "next_page_id": None,
                    },
                )
            if request.url.path == "/api/v1/settings":
                return httpx.Response(
                    200,
                    json={
                        "agent_settings": {
                            "mcp_config": {
                                "github": {
                                    "transport": "http",
                                    "url": mcp_url,
                                    "auth": {
                                        "strategy": "api_key",
                                        "value": "**********",
                                    },
                                }
                            }
                        }
                    },
                )
            if request.url.path == "/api/v1/settings/mcp/github/test":
                seen_probe = True
                assert request.extensions["timeout"]["read"] > 15
                return httpx.Response(
                    200,
                    json={"ok": False, "error": sentinel, "error_kind": "connection"},
                )
            raise AssertionError(f"Unexpected outbound request: {request.url}")

        await install_outbound_transport(outbound)
        response = await async_client.post(
            VALIDATE_URL,
            json=preflight(
                {**CRON_DRAFT, "repos": None},
                requirements={
                    "integrations": [
                        integration_requirement(
                            "github",
                            transport="shttp",
                            locator=mcp_url,
                            auth_strategy="api_key",
                            secret_names=["GITHUB_TOKEN"],
                        )
                    ]
                },
            ),
        )

        assert seen_probe is True
        assert response.status_code == 200
        body = response.json()
        assert [(error["code"], error["step"]) for error in body["errors"]] == [
            ("integration_unavailable", "prerequisites")
        ]
        assert sentinel not in response.text

    async def test_cloud_preflight_accepts_usable_integration_and_repository(
        self, async_client
    ):
        """A usable credential, MCP server, repository, and ref pass together."""
        mcp_url = "https://mcp.example.test/github"
        calls: list[str] = []

        def outbound(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path == "/api/v1/secrets/search":
                return httpx.Response(
                    200,
                    json={
                        "items": [{"name": "GITHUB_TOKEN", "description": None}],
                        "next_page_id": None,
                    },
                )
            if request.url.path == "/api/v1/settings":
                return httpx.Response(
                    200,
                    json={
                        "agent_settings": {
                            "mcp_config": {
                                "github": {
                                    "transport": "http",
                                    "url": mcp_url,
                                    "auth": {
                                        "strategy": "api_key",
                                        "value": "**********",
                                    },
                                }
                            }
                        }
                    },
                )
            if request.url.path == "/api/v1/settings/mcp/github/test":
                return httpx.Response(200, json={"ok": True})
            if request.url.path == "/api/v1/git/repositories/search":
                assert request.url.params["provider"] == "github"
                assert request.url.params["query"] == "OpenHands/agent-server-gui"
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "id": "1",
                                "full_name": "OpenHands/agent-server-gui",
                                "git_provider": "github",
                                "is_public": True,
                            }
                        ],
                        "next_page_id": None,
                    },
                )
            if request.url.path == "/api/v1/git/branches/search":
                assert request.url.params["repository"] == (
                    "OpenHands/agent-server-gui"
                )
                assert request.url.params["query"] == ""
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "name": "main",
                                "commit_sha": "0123456789abcdef",
                                "protected": True,
                            }
                        ],
                        "next_page_id": None,
                    },
                )
            raise AssertionError(f"Unexpected outbound request: {request.url}")

        await install_outbound_transport(outbound)
        response = await async_client.post(
            VALIDATE_URL,
            json=preflight(
                CRON_DRAFT,
                requirements={
                    "integrations": [
                        integration_requirement(
                            "github",
                            transport="shttp",
                            locator=mcp_url,
                            auth_strategy="api_key",
                            secret_names=["GITHUB_TOKEN"],
                        )
                    ]
                },
            ),
        )

        assert response.status_code == 200
        assert response.json()["valid"] is True
        assert response.json()["errors"] == []
        assert "/api/v1/settings/mcp/github/test" in calls
        assert "/api/v1/git/branches/search" in calls

    async def test_cloud_repository_and_ref_search_follow_pagination(
        self, async_client
    ):
        """An exact repository or ref on a later page is still accessible."""
        calls: list[tuple[str, str | None]] = []

        def outbound(request: httpx.Request) -> httpx.Response:
            page_id = request.url.params.get("page_id")
            calls.append((request.url.path, page_id))
            if request.url.path == "/api/v1/git/repositories/search":
                assert request.url.params["limit"] == "99"
                if page_id is None:
                    return httpx.Response(
                        200,
                        json={
                            "items": [],
                            "next_page_id": "repository-page-2",
                        },
                    )
                assert page_id == "repository-page-2"
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "id": "1",
                                "full_name": "OpenHands/agent-server-gui",
                                "git_provider": "github",
                                "is_public": False,
                            }
                        ],
                        "next_page_id": None,
                    },
                )
            if request.url.path == "/api/v1/git/branches/search":
                # The real Cloud route rejects paged, non-empty branch queries.
                if page_id is not None and request.url.params["query"]:
                    return httpx.Response(400)
                assert request.url.params["limit"] == "99"
                if page_id is None:
                    return httpx.Response(
                        200,
                        json={"items": [], "next_page_id": "branch-page-2"},
                    )
                assert page_id == "branch-page-2"
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "name": "main",
                                "commit_sha": "0123456789abcdef",
                                "protected": True,
                            }
                        ],
                        "next_page_id": None,
                    },
                )
            raise AssertionError(f"Unexpected outbound request: {request.url}")

        await install_outbound_transport(outbound)
        response = await async_client.post(
            VALIDATE_URL,
            json=preflight(CRON_DRAFT, requirements={"integrations": []}),
        )

        assert response.status_code == 200
        assert response.json()["valid"] is True
        assert calls == [
            ("/api/v1/git/repositories/search", None),
            ("/api/v1/git/repositories/search", "repository-page-2"),
            ("/api/v1/git/branches/search", None),
            ("/api/v1/git/branches/search", "branch-page-2"),
        ]

    async def test_repeated_repository_page_token_fails_closed(self, async_client):
        """A broken dependency cannot make preflight loop or claim success."""
        calls = 0

        def outbound(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            assert request.url.path == "/api/v1/git/repositories/search"
            calls += 1
            return httpx.Response(
                200,
                json={"items": [], "next_page_id": "repeated-page"},
            )

        await install_outbound_transport(outbound)
        response = await async_client.post(
            VALIDATE_URL,
            json=preflight(CRON_DRAFT, requirements={"integrations": []}),
        )

        assert calls == 2
        assert response.status_code == 503
        assert response.json() == {
            "detail": "Preflight validation is temporarily unavailable."
        }

    @pytest.mark.parametrize(
        "url",
        [
            "https://attacker.example/OpenHands/agent-server-gui",
            "git@attacker.example:OpenHands/agent-server-gui",
        ],
    )
    async def test_repository_provider_must_match_the_selected_host(
        self, async_client, url
    ):
        """A declared provider cannot validate a different host's same path."""

        def outbound(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/git/repositories/search":
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "full_name": "OpenHands/agent-server-gui",
                                "git_provider": "github",
                            }
                        ],
                        "next_page_id": None,
                    },
                )
            if request.url.path == "/api/v1/git/branches/search":
                return httpx.Response(
                    200,
                    json={
                        "items": [{"name": "main", "commit_sha": "abc123"}],
                        "next_page_id": None,
                    },
                )
            raise AssertionError(f"Unexpected outbound request: {request.url}")

        await install_outbound_transport(outbound)
        draft = {
            **CRON_DRAFT,
            "repos": [{"url": url, "provider": "github"}],
        }
        response = await async_client.post(
            VALIDATE_URL,
            json=preflight(draft, requirements={"integrations": []}),
        )

        assert response.status_code == 200
        assert addressed_errors(response.json()) == [
            ("repos[0].url", "repository_provider_unsupported")
        ]

    @pytest.mark.parametrize(
        ("repository_items", "expected_error"),
        [
            pytest.param(
                [],
                ("repos[0].url", "repository_not_accessible"),
                id="repository",
            ),
            pytest.param(
                [
                    {
                        "id": "1",
                        "full_name": "OpenHands/agent-server-gui",
                        "git_provider": "github",
                        "is_public": False,
                    }
                ],
                ("repos[0].url", "repository_provider_not_connected"),
                id="provider-disconnected-during-ref-check",
            ),
        ],
    )
    async def test_cloud_repository_failures_are_field_addressable(
        self,
        async_client,
        repository_items,
        expected_error,
    ):
        """Repository access failures identify the exact form field to fix."""

        def outbound(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/git/repositories/search":
                return httpx.Response(
                    200,
                    json={"items": repository_items, "next_page_id": None},
                )
            if request.url.path == "/api/v1/git/branches/search":
                return httpx.Response(403)
            raise AssertionError(f"Unexpected outbound request: {request.url}")

        await install_outbound_transport(outbound)
        response = await async_client.post(
            VALIDATE_URL,
            json=preflight(CRON_DRAFT, requirements={"integrations": []}),
        )

        assert response.status_code == 200
        assert addressed_errors(response.json()) == [expected_error]

    @pytest.mark.parametrize("ref", ["v1.0.0", "a" * 40, "missing-branch"])
    async def test_unverified_cloud_ref_uses_unsupported_advisory(
        self, async_client, ref
    ):
        def outbound(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/git/repositories/search":
                return httpx.Response(
                    200,
                    json={"items": [{"full_name": "OpenHands/agent-server-gui"}]},
                )
            assert request.url.path == "/api/v1/git/branches/search"
            return httpx.Response(200, json={"items": []})

        await install_outbound_transport(outbound)
        response = await async_client.post(
            VALIDATE_URL,
            json=preflight(
                {**CRON_DRAFT, "repos": [{**CRON_DRAFT["repos"][0], "ref": ref}]},
                requirements={"integrations": []},
            ),
        )

        # Canvas already treats 501 as advisory, not a passed validation. Do not
        # require users to discard a valid tag/commit pin to create an automation.
        assert response.status_code == 501
        assert "tag" in response.json()["detail"]
        assert "commit" in response.json()["detail"]
        assert "valid" not in response.json()

    @pytest.mark.parametrize("unverified_first", [True, False])
    async def test_unverified_ref_does_not_hide_another_repository_error(
        self, async_client, unverified_first
    ):
        """An unsupported ref check cannot turn a denied repository into advisory."""
        repos = [
            {"url": "owner/allowed", "provider": "github", "ref": "v1.0.0"},
            {"url": "owner/denied", "provider": "github"},
        ]
        if not unverified_first:
            repos.reverse()

        def outbound(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/repositories/search"):
                allowed = request.url.params["query"] == "owner/allowed"
                return httpx.Response(
                    200,
                    json={"items": [{"full_name": "owner/allowed"}] if allowed else []},
                )
            assert request.url.path.endswith("/branches/search")
            return httpx.Response(200, json={"items": []})

        await install_outbound_transport(outbound)
        response = await async_client.post(
            VALIDATE_URL,
            json=preflight(
                {**CRON_DRAFT, "repos": repos}, requirements={"integrations": []}
            ),
        )

        assert response.status_code == 200
        assert response.json()["valid"] is False
        denied_index = 1 if unverified_first else 0
        assert addressed_errors(response.json()) == [
            (f"repos[{denied_index}].url", "repository_not_accessible")
        ]

    async def test_unverified_ref_does_not_hide_trigger_or_credential_errors(
        self, async_client
    ):
        def outbound(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/repositories/search"):
                return httpx.Response(
                    200, json={"items": [{"full_name": "OpenHands/agent-server-gui"}]}
                )
            if request.url.path == "/api/v1/settings":
                return httpx.Response(
                    200,
                    json={
                        "agent_settings": {
                            "mcp_config": {
                                "example": {"url": "https://mcp.example.test"}
                            }
                        }
                    },
                )
            assert request.url.path in {
                "/api/v1/secrets/search",
                "/api/v1/git/branches/search",
            }
            return httpx.Response(200, json={"items": []})

        await install_outbound_transport(outbound)
        response = await async_client.post(
            VALIDATE_URL,
            json=preflight(
                with_trigger(CRON_DRAFT, schedule="*/10 * * * * *"),
                requirements={
                    "integrations": [
                        integration_requirement(
                            "example",
                            transport="shttp",
                            locator="https://mcp.example.test",
                            secret_names=["TOKEN"],
                        )
                    ]
                },
            ),
        )

        assert response.status_code == 200
        assert response.json()["valid"] is False
        assert addressed_errors(response.json()) == [
            ("trigger.schedule", "interval_too_short"),
            (None, "credential_missing"),
        ]

    @pytest.mark.parametrize(
        "failure",
        [
            404,
            501,
            429,
            500,
            "transport",
            "non_json",
            "malformed",
            "repeated",
            "truncated",
        ],
    )
    async def test_branch_dependency_failure_is_not_an_unsupported_ref(
        self, async_client, failure
    ):
        """Only a completed branch search permits advisory; outages still block."""
        calls = 0
        sentinel = "private-provider-body-and-token"

        def outbound(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            if request.url.path.endswith("/repositories/search"):
                return httpx.Response(
                    200, json={"items": [{"full_name": "OpenHands/agent-server-gui"}]}
                )
            assert request.url.path.endswith("/branches/search")
            calls += 1
            if isinstance(failure, int):
                return httpx.Response(failure, text=sentinel)
            if failure == "transport":
                raise httpx.ReadTimeout(sentinel, request=request)
            if failure == "non_json":
                return httpx.Response(200, text=sentinel)
            if failure == "malformed":
                return httpx.Response(200, json={"items": [{"name": [sentinel]}]})
            return httpx.Response(
                200,
                json={
                    "items": [],
                    "next_page_id": "same" if failure == "repeated" else str(calls),
                },
            )

        await install_outbound_transport(outbound)
        response = await async_client.post(
            VALIDATE_URL,
            json=preflight(CRON_DRAFT, requirements={"integrations": []}),
        )

        assert response.status_code == 503
        assert response.json() == {
            "detail": "Preflight validation is temporarily unavailable."
        }
        assert sentinel not in response.text
        assert calls <= capabilities_router._MAX_PREFLIGHT_SEARCH_PAGES

    @pytest.mark.parametrize("provider", ["github", "gitlab", "bitbucket"])
    @pytest.mark.parametrize("canonical_present", [True, False])
    @pytest.mark.parametrize("public", [True, False])
    async def test_local_repository_credentials_match_real_sdk_clone_lookup(
        self,
        async_client,
        monkeypatch,
        tmp_path,
        caplog,
        provider,
        canonical_present,
        public,
    ):
        """The pinned SDK reads canonical secrets, not environment/MCP aliases."""
        host = "http://agent-server.test"
        monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", host)
        monkeypatch.setenv("AUTOMATION_AGENT_SERVER_API_KEY", "session-key")
        monkeypatch.setenv(
            f"OPENHANDS_{provider.upper()}_TOKEN", "environment-token-sentinel"
        )
        clear_config_cache()
        canonical_name = sdk_repo.PROVIDER_TOKEN_NAMES[sdk_repo.GitProvider(provider)]
        token = "canonical-token-sentinel"
        # An integration's custom secret and the exported spelling must not
        # substitute for the provider credential RemoteWorkspace actually reads.
        stored = {
            f"{provider.upper()}_TOKEN": "mcp-token-sentinel",
            f"OPENHANDS_{provider.upper()}_TOKEN": "exported-token-sentinel",
        }
        if canonical_present:
            stored[canonical_name] = token
        repo = {"url": "owner/repo", "provider": provider}
        expected_token = token if canonical_present else None
        clone_tokens: list[str | None] = []

        def clone(_repo, _destination, credential):
            clone_tokens.append(credential)
            return public or credential == token

        def sdk_transport(request: httpx.Request) -> httpx.Response:
            assert request.headers["x-session-api-key"] == "session-key"
            assert request.url.path == f"/api/settings/secrets/{canonical_name}"
            return (
                httpx.Response(200, text=token)
                if canonical_present
                else httpx.Response(404)
            )

        monkeypatch.setattr(sdk_repo, "_clone_single_repo", clone)
        workspace = RemoteWorkspace(
            host=host, api_key="session-key", working_dir=str(tmp_path)
        )
        with httpx.Client(
            base_url=host, transport=httpx.MockTransport(sdk_transport)
        ) as client:
            workspace._client = client
            clone_result = workspace.clone_repos([repo])
        assert clone_tokens == [expected_token]
        assert clone_result.success_count == int(public or canonical_present)

        def preflight_transport(request: httpx.Request) -> httpx.Response:
            assert request.headers["x-session-api-key"] == "session-key"
            if request.url.path == "/api/settings/secrets":
                return httpx.Response(
                    200, json={"secrets": [{"name": name} for name in stored]}
                )
            # Preflight never reads the per-secret value endpoint used by clone.
            assert request.url.path == "/api/git/validate-repository"
            payload = json.loads(request.content)
            names = payload["credential_names"]
            assert names == ([canonical_name] if canonical_present else [])
            credential = next(
                (stored[name] for name in names if stored.get(name)), None
            )
            assert credential == expected_token
            return httpx.Response(
                200,
                json={
                    "status": "accessible"
                    if public or credential == token
                    else "not_found"
                },
            )

        await install_outbound_transport(preflight_transport)
        response = await async_client.post(
            VALIDATE_URL,
            json=preflight(
                {**CRON_DRAFT, "repos": [repo]}, requirements={"integrations": []}
            ),
        )

        assert response.status_code == 200
        assert response.json()["valid"] is bool(clone_result.success_count)
        for secret in [*stored.values(), "environment-token-sentinel"]:
            assert secret not in response.text
            assert secret not in caplog.text

    async def test_local_preflight_uses_names_and_encrypted_mcp_configuration(
        self, async_client, monkeypatch
    ):
        """Local secrets stay in agent-server; automation forwards encrypted config."""
        monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", "http://agent-server.test")
        monkeypatch.setenv("AUTOMATION_AGENT_SERVER_API_KEY", "session-key")
        clear_config_cache()
        mcp_url = "https://mcp.example.test/github"
        encrypted = "enc:v1:not-a-plaintext-secret"

        def outbound(request: httpx.Request) -> httpx.Response:
            assert request.headers["x-session-api-key"] == "session-key"
            if request.url.path == "/api/settings/secrets":
                return httpx.Response(
                    200,
                    json={
                        "secrets": [
                            {"name": "GITHUB_TOKEN", "description": None},
                            {"name": "github_token", "description": None},
                        ]
                    },
                )
            if request.url.path == "/api/settings":
                assert request.headers["x-expose-secrets"] == "encrypted"
                return httpx.Response(
                    200,
                    json={
                        "agent_settings": {
                            "mcp_config": {
                                "github": {
                                    "transport": "http",
                                    "url": mcp_url,
                                    "auth": {
                                        "strategy": "api_key",
                                        "value": encrypted,
                                    },
                                }
                            }
                        }
                    },
                )
            if request.url.path == "/api/mcp/test":
                payload = json.loads(request.content)
                assert payload["server"]["auth"]["value"] == encrypted
                return httpx.Response(200, json={"ok": True, "tools": []})
            if request.url.path == "/api/git/validate-repository":
                payload = json.loads(request.content)
                assert payload["credential_names"] == [
                    "github_token",
                ]
                return httpx.Response(200, json={"status": "accessible"})
            raise AssertionError(f"Unexpected outbound request: {request.url}")

        await install_outbound_transport(outbound)
        response = await async_client.post(
            VALIDATE_URL,
            json=preflight(
                CRON_DRAFT,
                requirements={
                    "integrations": [
                        integration_requirement(
                            "github",
                            transport="shttp",
                            locator=mcp_url,
                            auth_strategy="api_key",
                            secret_names=["GITHUB_TOKEN"],
                        )
                    ]
                },
            ),
        )

        assert response.status_code == 200
        assert response.json()["valid"] is True
        assert encrypted not in response.text

    @pytest.mark.parametrize(
        "failure_kind",
        [
            "status",
            "transport",
            "missing",
            "unsupported",
            "rate_limit",
            "non_json",
            "json_array",
            "invalid_items",
        ],
    )
    async def test_dependency_failures_return_sanitized_503(
        self, async_client, failure_kind
    ):
        """A dependency outage blocks creation and never surfaces its body."""
        sentinel = "provider-internal-stack-and-secret"

        def outbound(request: httpx.Request) -> httpx.Response:
            if failure_kind == "transport":
                raise httpx.ConnectError(sentinel, request=request)
            if failure_kind in {"missing", "unsupported", "rate_limit"}:
                return httpx.Response(
                    {"missing": 404, "unsupported": 501, "rate_limit": 429}[
                        failure_kind
                    ],
                    text=sentinel,
                )
            if failure_kind == "non_json":
                return httpx.Response(200, text=sentinel)
            if failure_kind == "json_array":
                return httpx.Response(200, json=[sentinel])
            if failure_kind == "invalid_items":
                return httpx.Response(200, json={"items": [{"full_name": None}]})
            return httpx.Response(500, text=sentinel)

        await install_outbound_transport(outbound)
        response = await async_client.post(
            VALIDATE_URL,
            json=preflight(CRON_DRAFT, requirements={"integrations": []}),
        )

        assert response.status_code == 503
        assert response.json() == {
            "detail": "Preflight validation is temporarily unavailable."
        }
        assert sentinel not in response.text

    async def test_preflight_total_timeout_fails_closed(
        self, async_client, monkeypatch
    ):
        """A slow dependency cannot retain an unbounded validation request."""
        monkeypatch.setattr(
            capabilities_router,
            "_PREFLIGHT_TOTAL_TIMEOUT_SECONDS",
            0.01,
            raising=False,
        )

        async def outbound(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0.05)
            if request.url.path == "/api/v1/git/repositories/search":
                return httpx.Response(
                    200,
                    json={
                        "items": [{"full_name": "OpenHands/agent-server-gui"}],
                        "next_page_id": None,
                    },
                )
            if request.url.path == "/api/v1/git/branches/search":
                return httpx.Response(
                    200,
                    json={
                        "items": [{"name": "main", "commit_sha": "abc123"}],
                        "next_page_id": None,
                    },
                )
            raise AssertionError(f"Unexpected outbound request: {request.url}")

        await install_outbound_transport(outbound)
        response = await async_client.post(
            VALIDATE_URL,
            json=preflight(CRON_DRAFT, requirements={"integrations": []}),
        )

        assert response.status_code == 503
        assert response.json() == {
            "detail": "Preflight validation is temporarily unavailable."
        }

    async def test_preflight_total_timeout_covers_event_source_lookup(
        self, async_client, monkeypatch
    ):
        """The request deadline also covers event checks before requirements."""
        monkeypatch.setattr(
            capabilities_router,
            "_PREFLIGHT_TOTAL_TIMEOUT_SECONDS",
            0.01,
            raising=False,
        )

        async def slow_webhook_lookup(*_args, **_kwargs):
            await asyncio.sleep(0.05)
            return None

        monkeypatch.setattr(
            capabilities_router,
            "get_webhook_config",
            slow_webhook_lookup,
        )

        response = await async_client.post(
            VALIDATE_URL,
            json=preflight(EVENT_DRAFT),
        )

        assert response.status_code == 503
        assert response.json() == {
            "detail": "Preflight validation is temporarily unavailable."
        }

    async def test_malformed_requirements_are_rejected_before_validation(
        self, async_client
    ):
        """The public requirements envelope is bounded and rejects secret values."""
        response = await async_client.post(
            VALIDATE_URL,
            json=preflight(
                CRON_DRAFT,
                requirements={
                    "integrations": [
                        {
                            "id": "github",
                            "alternatives": [
                                {
                                    "transport": "shttp",
                                    "locator": "https://mcp.example.test/github",
                                    "authStrategy": "api_key",
                                    "secretNames": ["GITHUB_TOKEN"],
                                    "secretValue": "must-not-be-accepted",
                                }
                            ],
                        }
                    ]
                },
            ),
        )

        assert response.status_code == 422
        assert "must-not-be-accepted" not in response.text

    @pytest.mark.parametrize("auth_method", [AuthMethod.COOKIE, AuthMethod.API_KEY])
    @pytest.mark.parametrize("credential_exists", [True, False])
    async def test_preflight_uses_authenticated_org_for_every_cloud_request(
        self, async_client, mock_authenticated_user, auth_method, credential_exists
    ):
        """A selected org's missing credential cannot pass using another org's data."""
        selected_org = str(mock_authenticated_user.org_id)
        user = dataclasses.replace(mock_authenticated_user, auth_method=auth_method)
        app.dependency_overrides[authenticate_request] = lambda: user
        calls: list[str] = []
        scopes: list[str | None] = []

        def outbound(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            scopes.append(request.headers.get("X-Org-Id"))
            scoped = request.headers.get("X-Org-Id") == selected_org
            if request.url.path == "/api/v1/secrets/search":
                present = credential_exists if scoped else not credential_exists
                return httpx.Response(
                    200, json={"items": [{"name": "TOKEN"}] if present else []}
                )
            if auth_method == AuthMethod.COOKIE:
                assert "unrelated" not in request.headers["cookie"]
                assert "keycloak_auth_1=second" in request.headers["cookie"]
            if request.url.path == "/api/v1/settings":
                return httpx.Response(
                    200,
                    json={
                        "agent_settings": {
                            "mcp_config": {
                                "example": {"url": "https://mcp.example.test"}
                            }
                        }
                    },
                )
            if request.url.path.endswith("/test"):
                return httpx.Response(200, json={"ok": True})
            if request.url.path.endswith("/repositories/search"):
                return httpx.Response(
                    200, json={"items": [{"full_name": "OpenHands/agent-server-gui"}]}
                )
            assert request.url.path.endswith("/branches/search")
            return httpx.Response(
                200, json={"items": [{"name": "main", "commit_sha": "a" * 40}]}
            )

        await install_outbound_transport(outbound)
        response = await async_client.post(
            VALIDATE_URL,
            headers={
                # API-key organization wins even when the request names another.
                "X-Org-Id": (
                    str(uuid.UUID(int=1))
                    if auth_method == AuthMethod.API_KEY
                    else selected_org
                ),
                "Cookie": (
                    "keycloak_auth=first; keycloak_auth_1=second; unrelated=private"
                ),
            },
            json=preflight(
                CRON_DRAFT,
                requirements={
                    "integrations": [
                        integration_requirement(
                            "example",
                            transport="shttp",
                            locator="https://mcp.example.test",
                            secret_names=["TOKEN"],
                        )
                    ]
                },
            ),
        )

        assert response.status_code == 200
        assert response.json()["valid"] is credential_exists
        assert addressed_errors(response.json()) == (
            [] if credential_exists else [(None, "credential_missing")]
        )
        assert set(scopes) == {selected_org}
        assert "/api/v1/git/branches/search" in calls
        assert ("/api/v1/settings/mcp/example/test" in calls) is credential_exists

    @pytest.mark.parametrize("ref", ["a", "abcdef", "abcdef0", "abcdef" + "0" * 34])
    async def test_hex_ref_does_not_pass_from_a_branch_tip_prefix(
        self, async_client, ref
    ):
        """A SHA prefix cannot distinguish a hex ref naming a tag from a commit."""

        def outbound(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/repositories/search"):
                return httpx.Response(
                    200, json={"items": [{"full_name": "OpenHands/agent-server-gui"}]}
                )
            return httpx.Response(
                200,
                json={"items": [{"name": "main", "commit_sha": "abcdef" + "0" * 34}]},
            )

        await install_outbound_transport(outbound)
        response = await async_client.post(
            VALIDATE_URL,
            json=preflight(
                {**CRON_DRAFT, "repos": [{**CRON_DRAFT["repos"][0], "ref": ref}]},
                requirements={"integrations": []},
            ),
        )
        assert response.status_code == 501

    @pytest.mark.parametrize("ref", [None, "main", "abcdef0"])
    async def test_cloud_default_and_exact_branch_names_pass(self, async_client, ref):
        def outbound(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/repositories/search"):
                return httpx.Response(
                    200, json={"items": [{"full_name": "OpenHands/agent-server-gui"}]}
                )
            assert ref is not None
            assert request.url.path.endswith("/branches/search")
            return httpx.Response(
                200, json={"items": [{"name": ref, "commit_sha": "b" * 40}]}
            )

        await install_outbound_transport(outbound)
        response = await async_client.post(
            VALIDATE_URL,
            json=preflight(
                {**CRON_DRAFT, "repos": [{**CRON_DRAFT["repos"][0], "ref": ref}]},
                requirements={"integrations": []},
            ),
        )

        assert response.status_code == 200
        assert response.json()["valid"] is True
        assert response.json()["errors"] == []

    @pytest.mark.parametrize(
        "locator",
        [
            "https://mcp.example.test:invalid",
            "https://mcp.example.test:65536",
            "https://mcp.example.test:0",
        ],
    )
    async def test_invalid_mcp_port_is_rejected(self, async_client, locator):
        response = await async_client.post(
            VALIDATE_URL,
            json=preflight(
                {**CRON_DRAFT, "repos": None},
                requirements={
                    "integrations": [
                        integration_requirement(
                            "example", transport="shttp", locator=locator
                        )
                    ]
                },
            ),
        )
        assert response.status_code == 422

    async def test_mcp_query_does_not_match_another_tenant(self, async_client):
        def outbound(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/settings":
                return httpx.Response(
                    200,
                    json={
                        "agent_settings": {
                            "mcp_config": {
                                "example": {
                                    "url": "https://mcp.example.test?tenant=other"
                                }
                            }
                        }
                    },
                )
            return httpx.Response(200, json={"ok": True})

        await install_outbound_transport(outbound)
        response = await async_client.post(
            VALIDATE_URL,
            json=preflight(
                {**CRON_DRAFT, "repos": None},
                requirements={
                    "integrations": [
                        integration_requirement(
                            "example",
                            transport="shttp",
                            locator="https://mcp.example.test?tenant=selected",
                        )
                    ]
                },
            ),
        )
        assert response.status_code == 200
        assert addressed_errors(response.json()) == [
            (None, "integration_not_configured")
        ]

    @pytest.mark.parametrize("failure", [False, True])
    async def test_concurrent_checks_finish_in_field_order_and_drain_on_failure(
        self, async_client, monkeypatch, failure
    ):
        """Probes overlap, keep field order, and never outlive the request."""
        both_started = asyncio.Event()
        started: set[str] = set()
        finished: set[str] = set()
        monkeypatch.setattr(capabilities_router, "_PREFLIGHT_TOTAL_TIMEOUT_SECONDS", 1)

        async def outbound(request: httpx.Request) -> httpx.Response:
            repository = request.url.params["query"]
            started.add(repository)
            if len(started) == 2:
                both_started.set()
            try:
                await both_started.wait()
                if failure:
                    if repository == "owner/first":
                        return httpx.Response(503, text="private-provider-detail")
                    await asyncio.Event().wait()
                return httpx.Response(200, json={"items": []})
            finally:
                finished.add(repository)

        await install_outbound_transport(outbound)
        response = await async_client.post(
            VALIDATE_URL,
            json=preflight(
                {
                    **CRON_DRAFT,
                    "repos": [
                        {"url": f"owner/{name}", "provider": "github"}
                        for name in ("first", "second")
                    ],
                },
                requirements={"integrations": []},
            ),
        )
        assert started == finished == {"owner/first", "owner/second"}
        if failure:
            assert response.status_code == 503
            assert "private-provider-detail" not in response.text
        else:
            assert response.status_code == 200
            assert addressed_errors(response.json()) == [
                (f"repos[{index}].url", "repository_not_accessible")
                for index in range(2)
            ]

    @pytest.mark.parametrize("finish", ["success", "cancel", "timeout"])
    async def test_probe_concurrency_is_bounded_and_cancellation_is_drained(
        self, async_client, monkeypatch, finish
    ):
        """Queued probes stop after cancellation; the client stays usable."""
        full = asyncio.Event()
        release = asyncio.Event()
        active = 0
        peak = 0
        started = 0
        limit = capabilities_router._PREFLIGHT_CONCURRENCY
        count = limit + 2
        if finish == "timeout":
            monkeypatch.setattr(
                capabilities_router, "_PREFLIGHT_TOTAL_TIMEOUT_SECONDS", 0.2
            )

        async def outbound(request: httpx.Request) -> httpx.Response:
            nonlocal active, peak, started
            started += 1
            active += 1
            peak = max(peak, active)
            if active == limit:
                full.set()
            try:
                await release.wait()
                return httpx.Response(200, json={"items": []})
            finally:
                active -= 1

        await install_outbound_transport(outbound)
        body = preflight(
            {
                **CRON_DRAFT,
                "repos": [
                    {"url": f"owner/repo-{index}", "provider": "github"}
                    for index in range(count)
                ],
            },
            requirements={"integrations": []},
        )
        task = asyncio.create_task(async_client.post(VALIDATE_URL, json=body))
        try:
            await asyncio.wait_for(full.wait(), timeout=2)
            assert active == limit
            assert started == limit
            if finish == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            elif finish == "timeout":
                response = await asyncio.wait_for(task, timeout=2)
                assert response.status_code == 503
            else:
                release.set()
                response = await task
                assert response.status_code == 200
                assert len(response.json()["errors"]) == count
            assert active == 0
            assert peak == limit
            if finish != "success":
                assert started == limit
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        retry = await async_client.post(VALIDATE_URL, json=body)
        assert retry.status_code == 200
        assert len(retry.json()["errors"]) == count
        assert active == 0

    @pytest.mark.parametrize("with_requirements", [True, False])
    async def test_repo_budget_is_field_addressable_and_preserves_legacy_clients(
        self, async_client, with_requirements
    ):
        def outbound(request: httpx.Request) -> httpx.Response:
            raise AssertionError(
                "An oversized preflight must not start dependency work"
            )

        await install_outbound_transport(outbound)
        body = preflight(
            {
                **CRON_DRAFT,
                "repos": [
                    {"url": f"owner/repo-{index}", "provider": "github"}
                    for index in range(
                        capabilities_router._MAX_PREFLIGHT_REPOSITORIES + 1
                    )
                ],
            }
        )
        if with_requirements:
            body["requirements"] = {"integrations": []}
        response = await async_client.post(VALIDATE_URL, json=body)
        assert response.status_code == 200
        assert addressed_errors(response.json()) == (
            [("repos", "too_many_repositories")] if with_requirements else []
        )

    @pytest.mark.parametrize("pagination", ["match", "end", "repeat", "limit"])
    async def test_secret_search_distinguishes_absence_from_incomplete_results(
        self, async_client, pagination
    ):
        pages = 0

        def outbound(request: httpx.Request) -> httpx.Response:
            nonlocal pages
            if request.url.path.endswith("/secrets/search"):
                pages += 1
                if pages == 2 and pagination in {"match", "end"}:
                    return httpx.Response(
                        200,
                        json={
                            "items": (
                                [{"name": "TOKEN"}] if pagination == "match" else []
                            )
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "items": [{"name": "TOKEN_SUFFIX"}],
                        "next_page_id": "same"
                        if pagination == "repeat"
                        else str(pages),
                    },
                )
            if request.url.path == "/api/v1/settings":
                return httpx.Response(
                    200,
                    json={
                        "agent_settings": {
                            "mcp_config": {
                                "example": {"url": "https://mcp.example.test"}
                            }
                        }
                    },
                )
            return httpx.Response(200, json={"ok": True})

        await install_outbound_transport(outbound)
        response = await async_client.post(
            VALIDATE_URL,
            json=preflight(
                {**CRON_DRAFT, "repos": None},
                requirements={
                    "integrations": [
                        integration_requirement(
                            "example",
                            transport="shttp",
                            locator="https://mcp.example.test",
                            secret_names=["TOKEN"],
                        )
                    ]
                },
            ),
        )
        if pagination in {"repeat", "limit"}:
            assert response.status_code == 503
            assert pages == (
                2
                if pagination == "repeat"
                else capabilities_router._MAX_PREFLIGHT_SEARCH_PAGES
            )
        else:
            assert response.status_code == 200
            assert response.json()["valid"] is (pagination == "match")
            assert pages == 2

    @pytest.mark.parametrize("available", [True, False])
    async def test_integration_alternative_can_recover_from_partial_failure(
        self, async_client, available
    ):
        """One broken stored connection must not mask a usable alternative."""
        calls = []

        def outbound(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/settings":
                return httpx.Response(
                    200,
                    json={
                        "agent_settings": {
                            "mcp_config": {
                                name: {"url": "https://mcp.example.test"}
                                for name in ("broken", "working")
                            }
                        }
                    },
                )
            calls.append(request.url.path)
            if request.url.path.endswith("/broken/test"):
                return httpx.Response(503, text="private-provider-error")
            return httpx.Response(200, json={"ok": available})

        await install_outbound_transport(outbound)
        response = await async_client.post(
            VALIDATE_URL,
            json=preflight(
                {**CRON_DRAFT, "repos": None},
                requirements={
                    "integrations": [
                        integration_requirement(
                            "example",
                            transport="shttp",
                            locator="https://mcp.example.test",
                        )
                    ]
                },
            ),
        )
        assert response.status_code == (200 if available else 503)
        if available:
            assert response.json()["valid"] is True
        assert len(calls) == 2
        assert "private-provider-error" not in response.text
