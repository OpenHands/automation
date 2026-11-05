"""Plugin sources in private repositories.

A source names its credential with a ``${NAME}`` placeholder in the URL user
info. These tests use a real git server that only serves clients holding its
token, and the SDK's real strict fetch, so a pass means git actually cloned
with the resolved credential.
"""

import json
import uuid
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from openhands.automation.config import clear_config_cache
from openhands.automation.plugin_secrets import (
    PluginSourceSecrets,
    local_plugin_source_secrets,
    plugin_source_secrets,
    preflight_secret_lookup,
)
from openhands.automation.plugin_specs import (
    AutomationPluginManifest,
    AutomationPluginSource,
    PluginSpecError,
    build_execution_payload,
    pin_plugin_refs,
    plugin_secret_names,
    plugin_source_secret_names,
    preflight_manifest,
    validate_required_plugins,
)
from openhands.sdk.secret import LookupSecret
from tests.private_git_server import PrivateGitServer, SecretStoreServer, make_bare_repo


TOKEN = "fake-git-token-0123456789"
SESSION_KEY = "fake-session-key"
USER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
ORG_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")

PLUGIN_MANIFEST = {
    "name": "private-plugin",
    "version": "0.1.0",
    "description": "A plugin in a private repository",
    "automation": {
        "schema_version": "openhands.automation.plugin.v1",
        "entrypoint": {"runner": "script", "command": "bash run.sh"},
    },
}


def _plugin_files(manifest: dict | None = None) -> dict[str, str]:
    return {
        ".plugin/plugin.json": json.dumps(manifest or PLUGIN_MANIFEST),
        "run.sh": "echo private plugin\n",
    }


def _source(server: PrivateGitServer, repo: str, user: str = "x-access-token") -> str:
    host = server.url.removeprefix("http://")
    return f"http://{user}:${{PLUGIN_TOKEN}}@{host}/{repo}.git"


@pytest.fixture
def private_repo(tmp_path):
    sha = make_bare_repo(tmp_path, "plugin", _plugin_files())
    with PrivateGitServer(tmp_path, TOKEN) as server:
        yield server, sha


@pytest.fixture
def secret_store():
    with SecretStoreServer(SESSION_KEY, {"PLUGIN_TOKEN": TOKEN}) as store:
        yield store


def _lookup(store: SecretStoreServer, name: str = "PLUGIN_TOKEN") -> LookupSecret:
    return LookupSecret(
        url=f"{store.url}/api/settings/secrets/{name}",
        headers={"X-Session-API-Key": SESSION_KEY},
    )


# ---------------------------------------------------------------------------
# The source contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "names"),
    [
        (
            "https://x-access-token:${GITHUB_TOKEN}@github.com/acme/private.git",
            ["GITHUB_TOKEN"],
        ),
        ("https://${GITHUB_TOKEN}@github.com/acme/private.git", ["GITHUB_TOKEN"]),
        (
            "https://oauth2:${GITLAB_TOKEN}@gitlab.com/group/plugin.git",
            ["GITLAB_TOKEN"],
        ),
        (
            "https://x-token-auth:${BITBUCKET_DATA_CENTER_TOKEN}"
            "@bitbucket.example.com:8443/context/scm/proj/plugin.git",
            ["BITBUCKET_DATA_CENTER_TOKEN"],
        ),
        (
            "https://alona%40example.com:${BB_PAT}@bitbucket.example.com/scm/p/r.git",
            ["BB_PAT"],
        ),
        (
            "https://${BB_USER}:${BB_TOKEN}@bitbucket.org/ws/repo.git",
            ["BB_TOKEN", "BB_USER"],
        ),
        ("github:acme/plugin", []),
        ("https://github.com/acme/plugin.git", []),
        ("/local/plugin", []),
    ],
)
def test_placeholder_sources_are_accepted_and_stored_unexpanded(source, names):
    plugin = AutomationPluginSource(source=source, ref="main")

    assert plugin.model_dump(exclude_none=True) == {"source": source, "ref": "main"}
    assert plugin_source_secret_names(plugin.source) == names


@pytest.mark.parametrize(
    "source",
    [
        "https://user:literal-password@github.com/acme/private.git",
        "https://ghp_literalToken@github.com/acme/private.git",
        "https://x-access-token:prefix${GITHUB_TOKEN}@github.com/acme/private.git",
        "https://x-access-token:${A}${B}@github.com/acme/private.git",
        "https://x-access-token:${GITHUB_TOKEN:-fallback}@github.com/acme/private.git",
        "https://user${A}:${B}@github.com/acme/private.git",
        "https://x-access-token:${T}@github.com/${ORG}/private.git",
        "https://x-access-token:${T}@${HOST}/acme/private.git",
        "https://github.com/acme/private.git?token=${T}",
        "github:user:token@acme/private",
        "github:${TOKEN}/acme/private",
        "/plugins/${NAME}",
    ],
)
def test_literal_credentials_and_misplaced_placeholders_are_rejected(source):
    with pytest.raises(ValidationError):
        AutomationPluginSource(source=source)


def test_ref_placeholders_are_rejected():
    with pytest.raises(ValidationError, match="ref must not contain"):
        AutomationPluginSource(
            source="https://x:${GITHUB_TOKEN}@github.com/a/b.git", ref="${GITHUB_TOKEN}"
        )


def test_required_plugins_follow_the_same_contract():
    automation = dict(PLUGIN_MANIFEST["automation"])
    manifest = {
        **automation,
        "name": "plugin",
        "runtime": {
            "plugins": [
                {"source": "https://oauth2:${GITLAB_TOKEN}@gitlab.com/g/helper.git"}
            ]
        },
    }
    parsed = AutomationPluginManifest.model_validate(manifest)
    assert plugin_secret_names(parsed.runtime.plugins) == ["GITLAB_TOKEN"]

    manifest["runtime"]["plugins"] = [
        {"source": "https://oauth2:glpat-literal@gitlab.com/g/helper.git"}
    ]
    with pytest.raises(ValidationError, match="must not include credentials"):
        AutomationPluginManifest.model_validate(manifest)


def test_secret_names_of_several_plugins_are_deduplicated():
    plugins = [
        {"source": "https://x:${GITHUB_TOKEN}@github.com/a/b.git"},
        AutomationPluginSource(source="https://x:${GITHUB_TOKEN}@github.com/a/c.git"),
        {"source": "https://oauth2:${GITLAB_TOKEN}@gitlab.com/g/h.git"},
        {"source": "github:acme/public"},
    ]
    assert plugin_secret_names(plugins) == ["GITHUB_TOKEN", "GITLAB_TOKEN"]


# ---------------------------------------------------------------------------
# Preflight against a private repository
# ---------------------------------------------------------------------------


def test_preflight_clones_with_a_secret_reference(private_repo, secret_store):
    server, sha = private_repo
    plugin = AutomationPluginSource(source=_source(server, "plugin"))

    resolved_ref, manifest = preflight_manifest(
        plugin, secrets={"PLUGIN_TOKEN": _lookup(secret_store)}
    )

    assert resolved_ref == sha
    assert manifest.name == "private-plugin"
    assert ("/plugin.git/info/refs", True) in server.requests
    assert secret_store.requests == ["/api/settings/secrets/PLUGIN_TOKEN"]


def test_preflight_passes_only_the_names_a_source_uses(private_repo, secret_store):
    server, _sha = private_repo
    unrelated = LookupSecret(url=f"{secret_store.url}/api/settings/secrets/OTHER")

    preflight_manifest(
        AutomationPluginSource(source=_source(server, "plugin")),
        secrets={"PLUGIN_TOKEN": _lookup(secret_store), "OTHER": unrelated},
    )

    assert secret_store.requests == ["/api/settings/secrets/PLUGIN_TOKEN"]


def test_public_source_preflight_is_unchanged(tmp_path, monkeypatch):
    plugin_dir = tmp_path / "public"
    for relative, content in _plugin_files().items():
        (plugin_dir / relative).parent.mkdir(parents=True, exist_ok=True)
        (plugin_dir / relative).write_text(content)
    seen = {}
    from openhands.automation import plugin_specs

    real = plugin_specs.preflight_plugin

    def spy(**kwargs):
        seen.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(plugin_specs, "preflight_plugin", spy)

    preflight_manifest(
        AutomationPluginSource(source=str(plugin_dir)),
        secrets={"PLUGIN_TOKEN": "unused"},
    )

    assert seen["secrets"] is None


def test_a_missing_secret_is_a_permanent_error_naming_it(private_repo, secret_store):
    server, _sha = private_repo
    plugin = AutomationPluginSource(source=_source(server, "plugin"))
    secret_store.secrets.clear()

    for secrets in ({}, {"PLUGIN_TOKEN": _lookup(secret_store)}):
        with pytest.raises(PluginSpecError) as exc:
            preflight_manifest(plugin, secrets=secrets)
        assert exc.value.code == "missing_secret"
        assert exc.value.status_code == 422
        assert "PLUGIN_TOKEN" in str(exc.value)


def test_a_rejected_secret_is_auth_failed_without_its_value(private_repo, secret_store):
    server, _sha = private_repo
    secret_store.secrets["PLUGIN_TOKEN"] = "fake-wrong-token-value"

    with pytest.raises(PluginSpecError) as exc:
        preflight_manifest(
            AutomationPluginSource(source=_source(server, "plugin")),
            secrets={"PLUGIN_TOKEN": _lookup(secret_store)},
        )

    assert exc.value.code == "auth_failed"
    assert exc.value.status_code == 422
    assert "PLUGIN_TOKEN" in str(exc.value)
    assert "fake-wrong-token-value" not in str(exc.value)
    assert TOKEN not in str(exc.value)


def test_an_unreachable_secret_store_is_transient(private_repo):
    server, _sha = private_repo
    unreachable = LookupSecret(
        url="http://127.0.0.1:9/api/settings/secrets/PLUGIN_TOKEN"
    )

    with pytest.raises(PluginSpecError) as exc:
        preflight_manifest(
            AutomationPluginSource(source=_source(server, "plugin")),
            secrets={"PLUGIN_TOKEN": unreachable},
        )

    assert exc.value.code == "secret_unavailable"
    assert exc.value.status_code == 503


def test_a_secret_value_that_breaks_the_url_is_permanent(private_repo, secret_store):
    """SDK 1.59.0 does not percent-encode the value; a "/" breaks the URL."""
    server, _sha = private_repo
    secret_store.secrets["PLUGIN_TOKEN"] = "fake/token/with/slashes"

    with pytest.raises(PluginSpecError) as exc:
        preflight_manifest(
            AutomationPluginSource(source=_source(server, "plugin")),
            secrets={"PLUGIN_TOKEN": _lookup(secret_store)},
        )

    assert exc.value.code == "invalid_source"
    assert exc.value.status_code == 422
    assert "PLUGIN_TOKEN" in str(exc.value)
    assert "with/slashes" not in str(exc.value)


def test_required_plugin_failures_keep_the_sdk_code(private_repo, secret_store):
    server, _sha = private_repo
    secret_store.secrets.clear()

    with pytest.raises(PluginSpecError) as exc:
        validate_required_plugins(
            [AutomationPluginSource(source=_source(server, "plugin"))],
            secrets={"PLUGIN_TOKEN": _lookup(secret_store)},
        )

    assert exc.value.code == "missing_secret"
    assert exc.value.status_code == 422
    assert "Failed to preflight required plugin" in str(exc.value)


def test_required_plugins_are_fetched_with_their_secrets(private_repo, secret_store):
    server, sha = private_repo

    [resolved] = validate_required_plugins(
        [AutomationPluginSource(source=_source(server, "plugin"), ref="main")],
        secrets={"PLUGIN_TOKEN": _lookup(secret_store)},
    )

    assert resolved == {
        "source": _source(server, "plugin"),
        "ref": "main",
        "resolved_ref": sha,
    }


async def test_execution_payload_resolves_secrets_for_primary_and_required_plugins(
    tmp_path,
):
    helper_sha = make_bare_repo(tmp_path, "helper", _plugin_files())
    with PrivateGitServer(tmp_path, TOKEN) as server:
        manifest = json.loads(json.dumps(PLUGIN_MANIFEST))
        manifest["automation"]["runtime"] = {
            "plugins": [{"source": _source(server, "helper", user="oauth2")}]
        }
        primary_sha = make_bare_repo(tmp_path, "primary", _plugin_files(manifest))
        requested: list[list[str]] = []

        async def secrets(names):
            requested.append(sorted(names))
            return {name: TOKEN for name in names}

        tarball, _env, runtime, fetches = await build_execution_payload(
            plugin_spec={"plugin": {"source": _source(server, "primary")}},
            agent_profile_id=None,
            agent_profile_name=None,
            llm_profile_name=None,
            work_dir="/workspace/project",
            secrets=secrets,
        )

    assert requested == [["PLUGIN_TOKEN"], ["PLUGIN_TOKEN"]]
    assert runtime["plugin_resolved_ref"] == primary_sha
    assert runtime["required_plugins"][0]["resolved_ref"] == helper_sha
    # Only the placeholder form is stored or sent on.
    assert fetches[0]["source"] == _source(server, "primary")
    assert TOKEN not in json.dumps(runtime)
    assert TOKEN.encode() not in tarball


async def test_pinning_a_private_plugin_uses_its_secrets(private_repo, secret_store):
    server, sha = private_repo

    async def secrets(names):
        return {name: _lookup(secret_store, name) for name in names}

    [pinned] = await pin_plugin_refs(
        [{"source": _source(server, "plugin"), "ref": "main"}],
        allow_local_sources=True,
        secrets=secrets,
    )

    assert pinned == {"source": _source(server, "plugin"), "ref": sha}


# ---------------------------------------------------------------------------
# Where the references come from
# ---------------------------------------------------------------------------


def test_local_references_point_at_the_agent_server_secret_store():
    secrets = local_plugin_source_secrets(
        ["GITHUB_TOKEN"],
        agent_server_url="http://localhost:3000/",
        api_key=SESSION_KEY,
        sandbox_agent_server_url="http://127.0.0.1:8000",
    )

    assert secrets.preflight["GITHUB_TOKEN"].url == (
        "http://localhost:3000/api/settings/secrets/GITHUB_TOKEN"
    )
    # The agent server resolves its own reference at the URL it serves itself.
    assert secrets.fetch["GITHUB_TOKEN"].url == (
        "http://127.0.0.1:8000/api/settings/secrets/GITHUB_TOKEN"
    )
    assert secrets.fetch_request_secrets(["GITHUB_TOKEN", "OTHER"]) == {
        "GITHUB_TOKEN": {
            "kind": "LookupSecret",
            "description": None,
            "url": "http://127.0.0.1:8000/api/settings/secrets/GITHUB_TOKEN",
            "headers": {"X-Session-API-Key": SESSION_KEY},
        }
    }


async def test_local_mode_resolves_through_the_agent_server(monkeypatch):
    monkeypatch.setenv("AUTOMATION_AGENT_SERVER_URL", "http://agent-server:8000")
    monkeypatch.setenv("AUTOMATION_AGENT_SERVER_API_KEY", SESSION_KEY)
    clear_config_cache()
    try:
        lookup = preflight_secret_lookup(
            httpx.AsyncClient(), user_id=USER_ID, org_id=ORG_ID
        )
        secrets = await lookup(["GITHUB_TOKEN"])
        assert await lookup([]) == {}
    finally:
        clear_config_cache()

    reference = secrets["GITHUB_TOKEN"]
    assert isinstance(reference, LookupSecret)
    assert reference.url == "http://agent-server:8000/api/settings/secrets/GITHUB_TOKEN"


class SecretLookupsApi:
    """The app server's secret-lookups service route (enterprise#1202)."""

    def __init__(self) -> None:
        self.response = httpx.Response(
            200,
            json={
                "lookup_url": "https://app.example.com/api/v1/webhooks/secrets",
                "lookup_path": "/api/v1/webhooks/secrets",
                "expires_at": "2026-11-05T12:00:00Z",
                "secrets": {
                    "GITHUB_TOKEN": {
                        "kind": "provider",
                        "provider": "github",
                        "headers": {"X-Access-Token": "fake-jws-github"},
                    },
                    "PLUGINS_PAT": {
                        "kind": "custom",
                        "headers": {"X-Access-Token": "fake-jws-pat"},
                    },
                },
                "missing": [],
            },
        )
        self.requests: list[httpx.Request] = []

    def respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@pytest.fixture
def cloud_mode(monkeypatch):
    monkeypatch.delenv("AUTOMATION_AGENT_SERVER_URL", raising=False)
    monkeypatch.setenv("AUTOMATION_SERVICE_KEY", "fake-service-key")
    monkeypatch.setenv(
        "AUTOMATION_OPENHANDS_API_BASE_URL", "http://openhands-service:3000"
    )
    clear_config_cache()
    api = SecretLookupsApi()
    yield api
    clear_config_cache()


async def _cloud(api: SecretLookupsApi, names: list[str]) -> PluginSourceSecrets:
    async with httpx.AsyncClient(transport=httpx.MockTransport(api.respond)) as client:
        return await plugin_source_secrets(
            client,
            names,
            user_id=USER_ID,
            org_id=ORG_ID,
            automation_id=uuid.UUID("33333333-3333-4333-8333-333333333333"),
        )


async def test_cloud_references_come_from_the_app_server(cloud_mode):
    secrets = await _cloud(cloud_mode, ["PLUGINS_PAT", "GITHUB_TOKEN"])

    [request] = cloud_mode.requests
    assert request.url == (
        f"http://openhands-service:3000/api/service/users/{USER_ID}"
        f"/orgs/{ORG_ID}/secret-lookups"
    )
    assert request.headers["X-Service-API-Key"] == "fake-service-key"
    assert json.loads(request.content) == {
        "names": ["GITHUB_TOKEN", "PLUGINS_PAT"],
        "purpose": "plugin_source",
        "automation_id": "33333333-3333-4333-8333-333333333333",
    }
    # The service resolves through its own API base URL; the sandbox through
    # the URL the app server gives it.
    assert secrets.preflight["GITHUB_TOKEN"].url == (
        "http://openhands-service:3000/api/v1/webhooks/secrets"
    )
    assert secrets.fetch["GITHUB_TOKEN"].url == (
        "https://app.example.com/api/v1/webhooks/secrets"
    )
    assert secrets.fetch["PLUGINS_PAT"].headers == {"X-Access-Token": "fake-jws-pat"}


@pytest.mark.parametrize(
    ("response", "code", "status_code", "message"),
    [
        (
            httpx.Response(404, json={"detail": "Not Found"}),
            "private_source_unsupported",
            422,
            "not supported on this deployment yet",
        ),
        (
            httpx.Response(405, json={"detail": "Method Not Allowed"}),
            "private_source_unsupported",
            422,
            "not supported on this deployment yet",
        ),
        (
            httpx.Response(404, json={"detail": "User x is not an active member"}),
            "secret_lookup_failed",
            404,
            "not an active member",
        ),
        (
            httpx.Response(401, json={"detail": "Invalid"}),
            "secret_lookup_failed",
            401,
            "HTTP 401",
        ),
        (httpx.Response(503), "secret_unavailable", 503, "HTTP 503"),
        (httpx.Response(429), "secret_unavailable", 429, "HTTP 429"),
        (httpx.Response(200, text="not json"), "secret_unavailable", 502, "Unexpected"),
        (
            httpx.ConnectError("connection refused"),
            "secret_unavailable",
            503,
            "Could not reach",
        ),
    ],
)
async def test_cloud_lookup_failures_are_classified(
    cloud_mode, response, code, status_code, message
):
    cloud_mode.response = response

    with pytest.raises(PluginSpecError) as exc:
        await _cloud(cloud_mode, ["GITHUB_TOKEN"])

    assert exc.value.code == code
    assert exc.value.status_code == status_code
    assert message in str(exc.value)


async def test_cloud_missing_names_are_a_missing_secret(cloud_mode):
    body = cloud_mode.response.json()
    body["missing"] = ["PLUGINS_PAT"]
    del body["secrets"]["PLUGINS_PAT"]
    cloud_mode.response = httpx.Response(200, json=body)

    with pytest.raises(PluginSpecError) as exc:
        await _cloud(cloud_mode, ["GITHUB_TOKEN", "PLUGINS_PAT"])

    assert exc.value.code == "missing_secret"
    assert exc.value.status_code == 422
    assert "PLUGINS_PAT" in str(exc.value)
    assert "GITHUB_TOKEN" not in str(exc.value)


async def test_cloud_skips_the_app_server_when_no_secret_is_named(cloud_mode):
    assert await _cloud(cloud_mode, []) == PluginSourceSecrets()
    assert cloud_mode.requests == []


async def test_cloud_backend_resolves_for_the_run_as_identity(cloud_mode):
    from types import SimpleNamespace

    from openhands.automation.backends.cloud import CloudSandboxBackend

    automation = SimpleNamespace(
        id=uuid.UUID("44444444-4444-4444-8444-444444444444"),
        user_id=USER_ID,
        org_id=ORG_ID,
    )
    backend = CloudSandboxBackend(
        api_url="http://openhands-service:3000",
        run=SimpleNamespace(automation=automation),  # type: ignore[arg-type]
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(cloud_mode.respond)
    ) as client:
        secrets = await backend.plugin_source_secrets(client, ["GITHUB_TOKEN"])
        assert await backend.plugin_source_secrets(client, []) == PluginSourceSecrets()

    assert f"/users/{USER_ID}/orgs/{ORG_ID}/" in str(cloud_mode.requests[0].url)
    assert json.loads(cloud_mode.requests[0].content)["automation_id"] == str(
        automation.id
    )
    assert set(secrets.fetch) == {"GITHUB_TOKEN"}


async def test_cloud_reference_resolves_inside_the_sdk_fetch(
    cloud_mode, private_repo, monkeypatch
):
    """The service's own preflight resolves a cloud reference through its API
    base URL plus the lookup path, with the reference's headers."""
    server, sha = private_repo
    with SecretStoreServer(SESSION_KEY, {"PLUGIN_TOKEN": TOKEN}) as lookup_server:
        monkeypatch.setenv("AUTOMATION_OPENHANDS_API_BASE_URL", lookup_server.url)
        clear_config_cache()
        body = cloud_mode.response.json()
        body["lookup_path"] = "/api/settings/secrets/PLUGIN_TOKEN"
        body["secrets"] = {
            "PLUGIN_TOKEN": {
                "kind": "custom",
                "headers": {"X-Session-API-Key": SESSION_KEY},
            }
        }
        cloud_mode.response = httpx.Response(200, json=body)

        secrets = await _cloud(cloud_mode, ["PLUGIN_TOKEN"])
        resolved_ref, _manifest = preflight_manifest(
            AutomationPluginSource(source=_source(server, "plugin")),
            allow_local_sources=True,
            secrets=secrets.preflight,
        )

    assert resolved_ref == sha
    assert lookup_server.requests == ["/api/settings/secrets/PLUGIN_TOKEN"]


def test_cloud_mode_rejects_http_sources_even_with_secrets():
    with pytest.raises(PluginSpecError, match="https://"):
        preflight_manifest(
            AutomationPluginSource(source="http://x:${T}@git.example.com/a/b.git"),
            allow_local_sources=False,
            secrets={"T": "unused"},
        )


def test_agent_server_payload_never_contains_a_value(tmp_path: Path):
    secrets = local_plugin_source_secrets(
        ["PLUGIN_TOKEN"], agent_server_url="http://a:1", api_key=SESSION_KEY
    )
    payload = json.dumps(secrets.fetch_request_secrets(["PLUGIN_TOKEN"]))
    assert TOKEN not in payload


@pytest.mark.parametrize("names", [["GITHUB_TOKEN"]])
async def test_cloud_backend_secrets_are_not_logged(cloud_mode, names, caplog):
    caplog.set_level("DEBUG")
    await _cloud(cloud_mode, names)
    assert "fake-jws-github" not in caplog.text
