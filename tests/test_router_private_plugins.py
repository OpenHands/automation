"""Creating and updating plugin-spec automations whose plugin is private.

The create and PATCH preflight reads the plugin with the secrets of the
identity the automation runs as. A real git server only serves its token.
"""

import json
import uuid

import httpx
import pytest
from sqlalchemy import select

from openhands.automation.app import app
from openhands.automation.config import clear_config_cache
from openhands.automation.models import Automation, AutomationRunAsKind
from tests.private_git_server import PrivateGitServer, SecretStoreServer, make_bare_repo


TOKEN = "fake-git-token-0123456789abcdef"
SESSION_KEY = "fake-agent-session-key"
CALLER_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
ORG_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")
SA_ID = uuid.UUID("99999999-9999-9999-9999-999999999998")
MANIFEST = {
    "name": "private-plugin",
    "version": "0.1.0",
    "automation": {
        "schema_version": "openhands.automation.plugin.v1",
        "entrypoint": {"runner": "script", "command": "bash run.sh"},
        "suggested_trigger": {"type": "cron", "schedule": "0 9 * * *"},
    },
}


@pytest.fixture
def private_plugin(tmp_path):
    sha = make_bare_repo(
        tmp_path,
        "plugin",
        {".plugin/plugin.json": json.dumps(MANIFEST), "run.sh": "echo ran\n"},
    )
    with PrivateGitServer(tmp_path, TOKEN) as server:
        host = server.url.removeprefix("http://")
        yield f"http://x-access-token:${{PLUGIN_TOKEN}}@{host}/plugin.git", sha


@pytest.fixture
def secret_store():
    with SecretStoreServer(SESSION_KEY, {"PLUGIN_TOKEN": TOKEN}) as store:
        yield store


@pytest.fixture
def local_mode(async_client, monkeypatch, secret_store):
    monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", secret_store.url)
    monkeypatch.setenv("AUTOMATION_AGENT_SERVER_API_KEY", SESSION_KEY)
    clear_config_cache()
    yield secret_store
    clear_config_cache()


class AppServer:
    """The OpenHands app server routes a cloud-mode create touches."""

    def __init__(self, lookup_server: SecretStoreServer) -> None:
        self.lookup_server = lookup_server
        self.lookups: list[httpx.Request] = []
        self.route_exists = True
        self.missing: list[str] = []
        self.members = {str(SA_ID): "service"}

    def respond(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/secret-lookups"):
            self.lookups.append(request)
            if not self.route_exists:
                return httpx.Response(404, json={"detail": "Not Found"})
            names = json.loads(request.content)["names"]
            return httpx.Response(
                200,
                json={
                    "lookup_url": self.lookup_server.url
                    + "/api/settings/secrets/PLUGIN_TOKEN",
                    "lookup_path": "/api/settings/secrets/PLUGIN_TOKEN",
                    "expires_at": "2026-11-05T12:00:00Z",
                    "secrets": {
                        name: {
                            "kind": "custom",
                            "headers": {"X-Session-API-Key": SESSION_KEY},
                        }
                        for name in names
                        if name not in self.missing
                    },
                    "missing": [name for name in names if name in self.missing],
                },
            )
        if path.endswith("/membership"):
            user_id = path.split("/")[4]
            if user_id not in self.members:
                return httpx.Response(404, json={"detail": "Not a member"})
            return httpx.Response(
                200,
                json={
                    "user_id": user_id,
                    "org_id": str(ORG_ID),
                    "account_type": self.members[user_id],
                    "role": "service_account",
                    "status": "active",
                },
            )
        if path == "/api/agent-profiles":
            return httpx.Response(200, json={"profiles": []})
        return httpx.Response(404, json={"detail": "Not Found"})


@pytest.fixture
async def cloud_mode(async_client, monkeypatch, secret_store):
    """Cloud mode, with the app server answering on the service's client.

    ``http://`` sources stay allowed only because the test git server speaks
    plain HTTP.
    """
    monkeypatch.delenv("AUTOMATION_AGENT_SERVER_URL", raising=False)
    monkeypatch.setenv("AUTOMATION_SERVICE_KEY", "fake-service-key")
    # The service resolves a reference through its own API base URL.
    monkeypatch.setenv("AUTOMATION_OPENHANDS_API_BASE_URL", secret_store.url)
    monkeypatch.setattr(
        "openhands.automation.router._allow_local_plugin_sources", lambda: True
    )
    clear_config_cache()
    api = AppServer(secret_store)
    previous = app.state.http_client
    app.state.http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(api.respond)
    )
    yield api
    await app.state.http_client.aclose()
    app.state.http_client = previous
    clear_config_cache()


async def _create(client, source, **extra):
    return await client.post(
        "/api/automation/v1/plugin-spec",
        json={"name": "Private plugin", "plugin": {"source": source}, **extra},
        headers={"Authorization": "Bearer caller-key"},
    )


async def test_local_create_reads_the_plugin_with_the_agent_server_secret(
    async_client, local_mode, private_plugin
):
    source, _sha = private_plugin

    response = await _create(async_client, source)

    assert response.status_code == 201, response.text
    assert response.json()["plugin_spec"]["plugin"] == {"source": source}
    assert local_mode.requests == ["/api/settings/secrets/PLUGIN_TOKEN"]
    assert TOKEN not in response.text


async def test_local_create_without_the_secret_is_rejected(
    async_client, local_mode, private_plugin
):
    source, _sha = private_plugin
    local_mode.secrets.clear()

    response = await _create(async_client, source)

    assert response.status_code == 422
    assert "Missing secret: PLUGIN_TOKEN" in response.json()["detail"]


async def test_create_rejects_literal_credentials(async_client, local_mode):
    response = await _create(
        async_client, "https://x-access-token:ghp_literal@github.com/acme/private.git"
    )

    assert response.status_code == 422
    assert "must not include credentials" in response.text


async def test_cloud_create_resolves_for_the_caller(
    async_client, cloud_mode, private_plugin
):
    source, _sha = private_plugin

    response = await _create(async_client, source)

    assert response.status_code == 201, response.text
    [lookup] = cloud_mode.lookups
    assert lookup.url.path == (
        f"/api/service/users/{CALLER_ID}/orgs/{ORG_ID}/secret-lookups"
    )
    assert json.loads(lookup.content)["names"] == ["PLUGIN_TOKEN"]
    assert response.json()["plugin_spec"]["plugin"] == {"source": source}
    assert TOKEN not in response.text


async def test_cloud_create_as_a_service_account_uses_its_secrets(
    async_client, cloud_mode, private_plugin
):
    source, sha = private_plugin

    response = await _create(async_client, source, run_as_user_id=str(SA_ID))

    assert response.status_code == 201, response.text
    assert cloud_mode.lookups[0].url.path == (
        f"/api/service/users/{SA_ID}/orgs/{ORG_ID}/secret-lookups"
    )
    # A service account stores the resolved SHA, still with the placeholder.
    assert response.json()["plugin_spec"]["plugin"] == {"source": source, "ref": sha}


async def test_cloud_create_without_the_lookup_route_is_not_supported(
    async_client, cloud_mode, private_plugin
):
    source, _sha = private_plugin
    cloud_mode.route_exists = False

    response = await _create(async_client, source)

    assert response.status_code == 422
    assert "not supported on this deployment yet" in response.json()["detail"]


async def test_cloud_create_with_a_missing_secret_names_it(
    async_client, cloud_mode, private_plugin
):
    source, _sha = private_plugin
    cloud_mode.missing = ["PLUGIN_TOKEN"]

    response = await _create(async_client, source)

    assert response.status_code == 422
    assert "Missing secret: PLUGIN_TOKEN" in response.json()["detail"]


async def test_cloud_public_create_never_asks_for_secrets(
    async_client, cloud_mode, tmp_path
):
    plugin = tmp_path / "public"
    (plugin / ".plugin").mkdir(parents=True)
    (plugin / ".plugin" / "plugin.json").write_text(json.dumps(MANIFEST))
    (plugin / "run.sh").write_text("echo ran\n")

    response = await _create(async_client, str(plugin))

    assert response.status_code == 201, response.text
    assert cloud_mode.lookups == []


async def test_patch_resolves_for_the_identity_the_automation_runs_as(
    async_client, async_session, cloud_mode, private_plugin, tmp_path
):
    source, _sha = private_plugin
    plugin = tmp_path / "public"
    (plugin / ".plugin").mkdir(parents=True)
    (plugin / ".plugin" / "plugin.json").write_text(json.dumps(MANIFEST))
    (plugin / "run.sh").write_text("echo ran\n")
    created = await _create(async_client, str(plugin))
    assert created.status_code == 201, created.text
    automation_id = uuid.UUID(created.json()["id"])
    # An admin edits an automation that runs as someone else.
    automation = (
        await async_session.execute(
            select(Automation).where(Automation.id == automation_id)
        )
    ).scalar_one()
    automation.user_id = SA_ID
    automation.run_as_kind = AutomationRunAsKind.USER
    await async_session.commit()

    response = await async_client.patch(
        f"/api/automation/v1/{automation_id}",
        json={"plugin": {"source": source}},
        headers={"Authorization": "Bearer caller-key"},
    )

    assert response.status_code == 200, response.text
    assert cloud_mode.lookups[-1].url.path == (
        f"/api/service/users/{SA_ID}/orgs/{ORG_ID}/secret-lookups"
    )
    assert response.json()["plugin_spec"]["plugin"] == {"source": source}


async def test_run_as_change_pins_with_the_new_identity_secrets(
    async_client, cloud_mode, private_plugin
):
    source, sha = private_plugin
    created = await _create(async_client, source)
    assert created.status_code == 201, created.text

    response = await async_client.post(
        f"/api/automation/v1/{created.json()['id']}/run-as",
        json={"to": "service", "service_account_id": str(SA_ID)},
        headers={"Authorization": "Bearer caller-key"},
    )

    assert response.status_code == 200, response.text
    assert cloud_mode.lookups[-1].url.path == (
        f"/api/service/users/{SA_ID}/orgs/{ORG_ID}/secret-lookups"
    )
    assert response.json()["plugin_spec"]["plugin"] == {"source": source, "ref": sha}
