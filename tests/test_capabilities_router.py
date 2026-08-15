"""Tests for the capabilities and preflight validation endpoints."""

import dataclasses
import json
import uuid

import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from openhands.automation.app import app
from openhands.automation.auth import authenticate_request
from openhands.automation.config import clear_config_cache
from openhands.automation.models import Base, CustomWebhook


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

GITHUB_USER = {"id": 3, "login": "someone"}


def with_trigger(draft: dict, **overrides: str) -> dict:
    """Copy a draft with individual trigger fields replaced."""
    return {**draft, "trigger": {**draft["trigger"], **overrides}}


def preflight(draft: dict, **extra: object) -> dict:
    """Build a preflight request body for the prompt-preset endpoint."""
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
async def async_engine():
    """Capabilities tests need SQLAlchemy, not an external Postgres service."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


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
                assert request.url.params["query"] == "main"
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

    @pytest.mark.parametrize(
        ("repository_items", "branch_items", "expected_error"),
        [
            pytest.param(
                [],
                None,
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
                [],
                ("repos[0].ref", "repository_ref_not_accessible"),
                id="ref",
            ),
        ],
    )
    async def test_cloud_repository_failures_are_field_addressable(
        self,
        async_client,
        repository_items,
        branch_items,
        expected_error,
    ):
        """Repository and ref failures identify the exact form field to fix."""

        def outbound(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/git/repositories/search":
                return httpx.Response(
                    200,
                    json={"items": repository_items, "next_page_id": None},
                )
            if request.url.path == "/api/v1/git/branches/search":
                assert branch_items is not None
                return httpx.Response(
                    200,
                    json={"items": branch_items, "next_page_id": None},
                )
            raise AssertionError(f"Unexpected outbound request: {request.url}")

        await install_outbound_transport(outbound)
        response = await async_client.post(
            VALIDATE_URL,
            json=preflight(CRON_DRAFT, requirements={"integrations": []}),
        )

        assert response.status_code == 200
        assert addressed_errors(response.json()) == [expected_error]

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
                    "GITHUB_TOKEN",
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

    @pytest.mark.parametrize("failure_kind", ["status", "transport"])
    async def test_dependency_failures_return_sanitized_503(
        self, async_client, failure_kind
    ):
        """A dependency outage blocks creation and never surfaces its body."""
        sentinel = "provider-internal-stack-and-secret"

        def outbound(request: httpx.Request) -> httpx.Response:
            if failure_kind == "transport":
                raise httpx.ConnectError(sentinel, request=request)
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
