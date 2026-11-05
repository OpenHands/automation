"""The service starts a cloud run's conversation through the OpenHands API.

On OpenHands Enterprise the user's agent profile, MCP servers and secrets live on
the app server, so a conversation the runner creates on the sandbox's own
agent-server gets none of them. The cloud backend asks the app server to start
the conversation in the run's sandbox instead, and the stock runners attach.
"""

import ast
import io
import json
import tarfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from openhands.automation.backends import CloudSandboxBackend, LocalAgentServerBackend
from openhands.automation.backends.base import (
    ConversationStart,
    ExecutionContext,
    StartedConversation,
)
from openhands.automation.dispatcher import (
    _conversation_start,
    _execute_run,
    _runner_repositories,
    _runner_takes_repository_list,
)
from openhands.automation.exceptions import (
    ConversationStartError,
    ConversationStartUnavailable,
)
from openhands.automation.models import (
    Automation,
    AutomationRun,
    AutomationRunStatus,
)
from openhands.automation.preset_router import (
    _generate_plugin_tarball,
    _generate_tarball,
)
from openhands.automation.subjects import conversation_id_for
from openhands.automation.utils import utcnow
from openhands.automation.utils.finish_plugin import (
    finish_plugin_dir,
    finish_plugin_files,
)
from openhands.automation.utils.tarball_validation import build_internal_url
from openhands.sdk.plugin import PluginSource
from openhands.workspace import RepoSource


TEST_USER_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
TEST_ORG_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")
PROFILE_ID = "0a1b2c3d-0000-4000-8000-000000000001"
PROFILE_UUID = uuid.UUID(PROFILE_ID)


@pytest.fixture
def mock_client():
    """The dispatcher's shared HTTP client; the backend is mocked around it."""
    return MagicMock()


TASK_ID = "7a1b2c3d-0000-4000-8000-00000000aaaa"
CONVERSATION_ID = "9f1b2c3d-0000-4000-8000-00000000cccc"
PRESETS_DIR = Path(__file__).parent.parent / "openhands" / "automation" / "presets"
FIXTURES = Path(__file__).parent / "fixtures" / "preset_tarballs"
# The owner's git connections, as ``provider_tokens_set`` reports them.
CONNECTED = {"github": None, "gitlab": "https://gitlab.com"}


class AppServer:
    """A fake OpenHands API: start task, task polling and the conversation read."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.post_status = 200
        self.post_detail: Any = None
        # Start task statuses answered to successive polls, after the POST.
        self.task_statuses = ["WORKING", "READY"]
        self.task_detail: str | None = None
        self.launched_profile: str | None = PROFILE_ID
        self.conversation_visible = True
        # Enterprise 1.81+ reports a conversation's additional repositories.
        self.reports_repositories = True

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "POST" and path == "/api/v1/app-conversations":
            if self.post_status != 200:
                return httpx.Response(
                    self.post_status, json={"detail": self.post_detail or "refused"}
                )
            return httpx.Response(200, json=self._task("WORKING"))
        if request.method == "GET" and path == "/api/v1/app-conversations/start-tasks":
            assert request.url.params["ids"] == TASK_ID
            status = self.task_statuses.pop(0) if self.task_statuses else "WORKING"
            return httpx.Response(200, json=[self._task(status)])
        if request.method == "GET" and path == "/api/v1/app-conversations":
            assert request.url.params["ids"] == CONVERSATION_ID
            if not self.conversation_visible:
                return httpx.Response(200, json=[None])
            launched = (
                {"agent_profile_id": self.launched_profile, "revision": 3}
                if self.launched_profile
                else None
            )
            conversation: dict[str, Any] = {
                "id": CONVERSATION_ID,
                "launched_agent_profile": launched,
            }
            if self.reports_repositories:
                conversation["additional_repositories"] = []
            return httpx.Response(200, json=[conversation])
        return httpx.Response(404, json={"detail": f"no route {path}"})

    def _task(self, status: str) -> dict[str, Any]:
        task: dict[str, Any] = {"id": TASK_ID, "status": status, "detail": None}
        if status == "READY":
            task["app_conversation_id"] = CONVERSATION_ID
        if status == "ERROR":
            task["detail"] = self.task_detail or "Sandbox not found"
        return task

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def posted_body(self) -> dict[str, Any]:
        post = next(r for r in self.requests if r.method == "POST")
        return json.loads(post.content)


def _backend(api_key: str = "sk-user") -> CloudSandboxBackend:
    run = MagicMock()
    run.sandbox_id = None
    run.bash_command_id = None
    run.automation.org_id = TEST_ORG_ID
    backend = CloudSandboxBackend(api_url="https://app.test", run=run)
    backend._api_key = api_key
    backend._poll_interval = 0
    backend._ready_timeout = 1
    return backend


def _ctx() -> ExecutionContext:
    return ExecutionContext(
        agent_url="http://agent.test",
        session_key="sk-session",
        sandbox_id="sbx-1",
        api_url="https://app.test",
        api_key="sk-user",
    )


def _start(**overrides: Any) -> ConversationStart:
    values: dict[str, Any] = {
        "agent_profile_id": PROFILE_ID,
        "llm_profile": "Default",
        "conversation_id": None,
        "plugins": [{"source": "github:acme/plugin", "ref": "abc123"}],
        "tags": {
            "automationtrigger": "automation",
            "automationid": "auto-1",
            "automationname": "Nightly",
            "automationrunid": "run-1",
        },
    }
    values.update(overrides)
    return ConversationStart(**values)


class TestCloudBackendStartsTheConversation:
    @pytest.mark.asyncio
    async def test_posts_the_sandbox_profile_plugins_and_tags(self):
        """The app server gets exactly what it needs: the run's sandbox, the
        profile, the plugins and the run tags. No prompt and no secrets: the
        runner sends the prompt, and the app server resolves secrets itself."""
        server = AppServer()
        async with server.client() as client:
            started = await _backend().start_conversation(client, _ctx(), _start())

        assert started == StartedConversation(
            conversation_id=CONVERSATION_ID, agent_profile_id=PROFILE_ID
        )
        assert server.posted_body() == {
            "sandbox_id": "sbx-1",
            "trigger": "automation",
            "agent_profile_id": PROFILE_ID,
            "llm_profile": "Default",
            "plugins": [{"source": "github:acme/plugin", "ref": "abc123"}],
            "tags": {
                "automationtrigger": "automation",
                "automationid": "auto-1",
                "automationname": "Nightly",
                "automationrunid": "run-1",
            },
        }
        post = next(r for r in server.requests if r.method == "POST")
        assert post.headers["Authorization"] == "Bearer sk-user"
        assert post.headers["X-Org-Id"] == str(TEST_ORG_ID)
        # WORKING on the first poll, READY on the second, then the read.
        assert [r.url.path for r in server.requests] == [
            "/api/v1/app-conversations",
            "/api/v1/app-conversations/start-tasks",
            "/api/v1/app-conversations/start-tasks",
            "/api/v1/app-conversations",
        ]

    @pytest.mark.asyncio
    async def test_omits_optional_fields_it_has_no_value_for(self):
        """Without a profile the app server applies the owner's active one; the
        request must not send ``null`` and have it read as an override."""
        server = AppServer()
        server.launched_profile = None
        async with server.client() as client:
            started = await _backend().start_conversation(
                client,
                _ctx(),
                _start(agent_profile_id=None, llm_profile=None, plugins=[]),
            )

        body = server.posted_body()
        assert "agent_profile_id" not in body
        assert "llm_profile" not in body
        assert "plugins" not in body
        assert "conversation_id" not in body
        assert started.agent_profile_id is None

    @pytest.mark.asyncio
    async def test_passes_the_derived_conversation_id_for_subject_runs(self):
        server = AppServer()
        async with server.client() as client:
            await _backend().start_conversation(
                client, _ctx(), _start(conversation_id=CONVERSATION_ID)
            )

        assert server.posted_body()["conversation_id"] == CONVERSATION_ID

    @pytest.mark.asyncio
    async def test_hands_the_repositories_to_the_app_server(self):
        """The app server clones them, runs their setup and loads their skills
        and instruction files, as for a chat. A clone failure fails the start,
        as it failed the run when the runner cloned."""
        server = AppServer()
        repositories = [
            {"repository": "acme/api", "git_provider": "github", "branch": "main"},
            {"repository": "group/web", "git_provider": "gitlab", "branch": None},
        ]
        async with server.client() as client:
            started = await _backend().start_conversation(
                client, _ctx(), _start(repositories=repositories)
            )

        body = server.posted_body()
        assert body["selected_repository"] == "acme/api"
        assert body["git_provider"] == "github"
        assert body["selected_branch"] == "main"
        assert body["additional_repositories"] == [
            {"repository": "group/web", "git_provider": "gitlab"}
        ]
        assert body["require_all"] is True
        assert started.reports_repositories

    @pytest.mark.asyncio
    async def test_a_single_repository_is_only_the_selected_one(self):
        server = AppServer()
        async with server.client() as client:
            await _backend().start_conversation(
                client,
                _ctx(),
                _start(
                    repositories=[{"repository": "acme/api", "git_provider": "github"}]
                ),
            )

        body = server.posted_body()
        assert body["selected_repository"] == "acme/api"
        assert "selected_branch" not in body
        assert "additional_repositories" not in body
        assert "require_all" not in body

    @pytest.mark.asyncio
    async def test_notices_an_app_server_that_clones_only_the_selected_one(self):
        """Before enterprise 1.81 the start ignores additional repositories and
        the conversation does not report them; the runner clones them then."""
        server = AppServer()
        server.reports_repositories = False
        async with server.client() as client:
            started = await _backend().start_conversation(client, _ctx(), _start())

        assert not started.reports_repositories

    @pytest.mark.parametrize(
        ("status", "detail"),
        [
            # FastAPI's own answers: no such route, or a body it cannot
            # validate, such as a trigger value it does not know.
            (404, "Not Found"),
            (405, "Method Not Allowed"),
            (
                422,
                [
                    {
                        "type": "enum",
                        "loc": ["body", "trigger"],
                        "msg": "Input should be 'gui' or 'resolver'",
                    }
                ],
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_an_app_server_without_the_contract_means_fallback(
        self, status, detail
    ):
        """An older app server has no such route or rejects the body; the run
        continues the old way rather than failing."""
        server = AppServer()
        server.post_status = status
        server.post_detail = detail
        async with server.client() as client:
            with pytest.raises(ConversationStartUnavailable) as exc_info:
                await _backend().start_conversation(client, _ctx(), _start())

        assert exc_info.value.status_code == status
        assert len(server.requests) == 1

    @pytest.mark.asyncio
    async def test_a_missing_route_without_json_means_fallback(self):
        def handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, text="<html>Not Found</html>")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as c:
            with pytest.raises(ConversationStartUnavailable):
                await _backend().start_conversation(c, _ctx(), _start())

    @pytest.mark.parametrize(
        ("status", "detail"),
        [
            (404, {"code": "agent_profile_not_found"}),
            (
                422,
                {
                    "code": "unresolved_profile_references",
                    "mcp_server_refs": ["gone"],
                },
            ),
            (422, "The LLM profile references a missing provider connection"),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_profile_the_app_server_cannot_launch_fails_the_run(
        self, status, detail
    ):
        """The app server answers 404/422 with its own detail when it cannot
        find or resolve the profile. Falling back would run the default agent
        instead of the one the automation selected."""
        server = AppServer()
        server.post_status = status
        server.post_detail = detail
        async with server.client() as client:
            with pytest.raises(ConversationStartError) as exc_info:
                await _backend().start_conversation(client, _ctx(), _start())

        exc = exc_info.value
        assert exc.status_code == status
        assert not exc.transient
        assert len(server.requests) == 1

    @pytest.mark.asyncio
    async def test_a_refusal_fails_the_run(self):
        server = AppServer()
        server.post_status = 400
        server.post_detail = "Profile 'Default' not found"
        async with server.client() as client:
            with pytest.raises(ConversationStartError) as exc_info:
                await _backend().start_conversation(client, _ctx(), _start())

        exc = exc_info.value
        assert exc.status_code == 400
        assert not exc.transient
        assert "Profile 'Default' not found" in str(exc)

    @pytest.mark.asyncio
    async def test_a_failed_start_task_fails_the_run(self):
        server = AppServer()
        server.task_statuses = ["ERROR"]
        server.task_detail = "Sandbox is not available in the requested organization"
        async with server.client() as client:
            with pytest.raises(ConversationStartError) as exc_info:
                await _backend().start_conversation(client, _ctx(), _start())

        exc = exc_info.value
        assert not exc.transient
        assert exc.code == "start_task_error"
        assert "requested organization" in str(exc)

    @pytest.mark.asyncio
    async def test_a_start_task_that_never_finishes_is_transient(self):
        server = AppServer()
        server.task_statuses = []  # WORKING forever
        backend = _backend()
        setattr(backend, "_ready_timeout", 0.002)
        setattr(backend, "_poll_interval", 0.001)
        async with server.client() as client:
            with pytest.raises(ConversationStartError) as exc_info:
                await backend.start_conversation(client, _ctx(), _start())

        assert exc_info.value.transient
        assert exc_info.value.code == "start_task_timeout"

    @pytest.mark.asyncio
    async def test_server_errors_propagate_for_transient_classification(self):
        server = AppServer()
        server.post_status = 503
        async with server.client() as client:
            with pytest.raises(httpx.HTTPStatusError):
                await _backend().start_conversation(client, _ctx(), _start())

    @pytest.mark.asyncio
    async def test_an_invisible_conversation_fails_the_run(self):
        server = AppServer()
        server.conversation_visible = False
        async with server.client() as client:
            with pytest.raises(ConversationStartError) as exc_info:
                await _backend().start_conversation(client, _ctx(), _start())

        assert exc_info.value.code == "conversation_missing"

    @pytest.mark.asyncio
    async def test_refreshes_the_key_once_on_an_auth_error(self):
        server = AppServer()
        seen: list[str] = []

        def handle(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers["Authorization"])
            if request.method == "POST" and len(seen) == 1:
                return httpx.Response(401, json={"detail": "expired"})
            return server.handle(request)

        backend = _backend(api_key="sk-old")
        with patch(
            "openhands.automation.backends.cloud.get_api_key_for_automation_run",
            new_callable=AsyncMock,
            return_value="sk-new",
        ):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as c:
                started = await backend.start_conversation(c, _ctx(), _start())

        assert started.conversation_id == CONVERSATION_ID
        assert seen[:2] == ["Bearer sk-old", "Bearer sk-new"]

    @pytest.mark.asyncio
    async def test_needs_a_sandbox(self):
        ctx = ExecutionContext(agent_url="http://agent.test", session_key="k")
        async with AppServer().client() as client:
            with pytest.raises(ConversationStartError):
                await _backend().start_conversation(client, ctx, _start())


class TestCloudBackendReadsTheOwnersGitConnections:
    @pytest.mark.asyncio
    async def test_reads_provider_tokens_set_from_the_settings(self):
        """Hosts only: the settings answer never carries a token here."""
        requests: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            assert request.url.path == "/api/v1/settings"
            return httpx.Response(
                200,
                json={
                    "provider_tokens_set": {
                        "github": None,
                        "gitlab": "gitlab.example.com",
                    },
                    "llm_api_key_set": True,
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            connections = await _backend().git_connections(client, _ctx())

        assert connections == {"github": None, "gitlab": "gitlab.example.com"}
        assert requests[0].headers["Authorization"] == "Bearer sk-user"
        assert requests[0].headers["X-Org-Id"] == str(TEST_ORG_ID)

    @pytest.mark.asyncio
    async def test_no_connections(self):
        def handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"provider_tokens_set": None})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            assert await _backend().git_connections(client, _ctx()) == {}

    @pytest.mark.asyncio
    async def test_a_refusal_raises(self):
        def handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"error": "Settings not found"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(ConversationStartError, match="git connections"):
                await _backend().git_connections(client, _ctx())


class TestLocalBackendLeavesCreationToTheRunner:
    @pytest.mark.asyncio
    async def test_returns_none(self):
        run = MagicMock()
        backend = LocalAgentServerBackend(
            agent_server_url="http://localhost:3000", api_key="k", run=run
        )
        ctx = await backend.get_execution_context(MagicMock())
        async with httpx.AsyncClient() as client:
            assert await backend.start_conversation(client, ctx, _start()) is None


def _automation(**overrides: Any) -> Automation:
    values: dict[str, Any] = {
        "id": uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001"),
        "name": "Nightly",
        "preset_metadata": {"preset_type": "prompt", "prompt": "hi"},
        "plugin_spec": None,
        "agent_profile_id": uuid.UUID(PROFILE_ID),
        "model": "Default",
    }
    values.update(overrides)
    return cast(Automation, SimpleNamespace(**values))


_RUN = cast(
    AutomationRun, SimpleNamespace(id=uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002"))
)


class TestWhichRunsStartThroughTheAppServer:
    """Only runs whose runner the service ships, and can therefore make attach."""

    def test_prompt_preset(self):
        start = _conversation_start(_automation(), _RUN, None, [], {})
        assert start == ConversationStart(
            agent_profile_id=PROFILE_ID,
            llm_profile="Default",
            conversation_id=None,
            # The presets require the finish tool; the start request takes
            # that requirement as a plugin the service writes into the sandbox.
            plugins=[{"source": finish_plugin_dir(str(_RUN.id))}],
            tags={
                "automationtrigger": "automation",
                "automationid": "aaaaaaaa-0000-4000-8000-000000000001",
                "automationname": "Nightly",
                "automationrunid": "bbbbbbbb-0000-4000-8000-000000000002",
            },
        )

    def test_prompt_preset_without_a_profile_uses_the_owners_active_one(self):
        start = _conversation_start(
            _automation(agent_profile_id=None, model=None), _RUN, None, [], {}
        )
        assert start is not None
        assert start.agent_profile_id is None
        assert start.llm_profile is None

    def test_plugin_preset_passes_its_plugins(self):
        plugins = [{"source": "github:acme/plugin", "ref": "v1"}]
        start = _conversation_start(
            _automation(preset_metadata={"preset_type": "plugin", "plugins": plugins}),
            _RUN,
            None,
            [],
            {},
        )
        assert start is not None
        assert start.plugins == [{"source": finish_plugin_dir(str(_RUN.id))}, *plugins]

    def test_preset_repositories_go_to_the_app_server_when_its_runner_allows(self):
        repos = [
            {"url": "https://github.com/acme/api", "ref": "main"},
            {"url": "git@github.com:acme/ssh.git"},
            {"url": "https://gitlab.com/group/web"},
        ]
        automation = _automation(
            preset_metadata={"preset_type": "prompt", "prompt": "hi", "repos": repos}
        )

        start = _conversation_start(
            automation,
            _RUN,
            None,
            [],
            {},
            runner_takes_repository_list=True,
            git_connections=CONNECTED,
        )
        assert start is not None
        assert start.repositories == [
            {"repository": "acme/api", "git_provider": "github", "branch": "main"},
            {"repository": "group/web", "git_provider": "gitlab", "branch": None},
        ]

        # An automation created before this support carries a runner that
        # clones every repository itself and would fail on existing checkouts.
        older = _conversation_start(
            automation, _RUN, None, [], {}, git_connections=CONNECTED
        )
        assert older is not None
        assert older.repositories == []

    def test_without_the_owners_connections_the_runner_clones_them_all(self):
        """The app server clones only through the owner's git connections; when
        those could not be read, it gets no repository."""
        automation = _automation(
            preset_metadata={
                "preset_type": "prompt",
                "prompt": "hi",
                "repos": [{"url": "https://github.com/acme/api"}],
            }
        )

        start = _conversation_start(
            automation, _RUN, None, [], {}, runner_takes_repository_list=True
        )
        assert start is not None
        assert start.repositories == []

    def test_plugin_preset_experiment_keeps_the_runner_path(self):
        """A/B variants pick plugins and model inside the runner at run time; the
        service cannot start that conversation for it."""
        start = _conversation_start(
            _automation(preset_metadata={"preset_type": "plugin", "prompt": "hi"}),
            _RUN,
            None,
            [],
            {},
        )
        assert start is None

    def test_plugin_spec_conversation_runner(self):
        fetches = [
            {"source": "github:acme/plugin", "ref": "abc", "repo_path": "p"},
            {"source": "github:acme/other", "ref": "def"},
        ]
        start = _conversation_start(
            _automation(
                preset_metadata={"preset_type": "plugin_spec"},
                plugin_spec={"plugin": {"source": "github:acme/plugin"}},
                agent_profile_id=None,
                model="Ignored",
            ),
            _RUN,
            {"runner": "conversation", "agent_profile_id": PROFILE_ID},
            fetches,
            {"AUTOMATION_CONVERSATION_ID": CONVERSATION_ID},
        )
        assert start is not None
        assert start.agent_profile_id == PROFILE_ID
        # The agent profile owns the model for plugin-spec runs.
        assert start.llm_profile is None
        assert start.plugins == fetches
        assert start.conversation_id == CONVERSATION_ID

    def test_plugin_spec_script_runner_is_left_alone(self):
        start = _conversation_start(
            _automation(
                preset_metadata={"preset_type": "plugin_spec"},
                plugin_spec={"plugin": {"source": "github:acme/plugin"}},
            ),
            _RUN,
            {"runner": "script", "agent_profile_id": PROFILE_ID},
            [],
            {},
        )
        assert start is None

    def test_custom_tarball_is_left_alone(self):
        assert (
            _conversation_start(_automation(preset_metadata=None), _RUN, None, [], {})
            is None
        )
        assert (
            _conversation_start(
                _automation(preset_metadata={"template": {"id": "x"}}),
                _RUN,
                None,
                [],
                {},
            )
            is None
        )


def _tarball(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


_RECORDED_REPOS = [
    {"url": "https://github.com/acme/api"},
    {"url": "git@github.com:acme/ssh.git"},
    {"url": "https://github.com/acme/web"},
    {"url": "https://github.com/acme/docs"},
]


class TestWhichRepositoriesTheRunnerClones:
    def _start_and_automation(self) -> tuple[ConversationStart, Automation]:
        automation = _automation(
            preset_metadata={
                "preset_type": "prompt",
                "prompt": "hi",
                "repos": _RECORDED_REPOS,
            }
        )
        start = _conversation_start(
            automation,
            _RUN,
            None,
            [],
            {},
            runner_takes_repository_list=True,
            git_connections=CONNECTED,
        )
        assert start is not None
        return start, automation

    def test_only_the_ones_the_app_server_cannot_clone(self):
        start, automation = self._start_and_automation()
        started = StartedConversation(
            conversation_id=CONVERSATION_ID, agent_profile_id=PROFILE_ID
        )

        assert _runner_repositories(automation, start, started, CONNECTED) == [
            {"url": "git@github.com:acme/ssh.git"}
        ]

    def test_and_the_additional_ones_an_older_app_server_ignored(self):
        start, automation = self._start_and_automation()
        started = StartedConversation(
            conversation_id=CONVERSATION_ID,
            agent_profile_id=PROFILE_ID,
            reports_repositories=False,
        )

        assert _runner_repositories(automation, start, started, CONNECTED) == [
            {"url": "git@github.com:acme/ssh.git"},
            {"url": "https://github.com/acme/web"},
            {"url": "https://github.com/acme/docs"},
        ]

    def test_new_preset_tarballs_take_a_list(self):
        repos = [RepoSource(url="https://github.com/acme/api")]
        assert _runner_takes_repository_list(_generate_tarball("hi", repos))
        assert _runner_takes_repository_list(
            _generate_plugin_tarball(
                [PluginSource(source="github:acme/plugin")], "hi", repos
            )
        )

    def test_older_tarballs_and_others_do_not(self):
        older = _tarball({"main.py": "print('clones repos_config.json')\n"})
        assert not _runner_takes_repository_list(older)
        assert not _runner_takes_repository_list(_tarball({"run.sh": "echo"}))
        assert not _runner_takes_repository_list(b"not a tarball")
        assert not _runner_takes_repository_list("https://example.com/code.tar.gz")
        assert not _runner_takes_repository_list(None)


@pytest.mark.asyncio
class TestExecuteRunStartsTheConversation:
    @pytest.fixture(autouse=True)
    def uploads(self):
        """Writes into the sandbox; the finish plugin goes there first."""
        with patch(
            "openhands.automation.dispatcher.upload_files", new_callable=AsyncMock
        ) as upload:
            yield upload

    async def _run(
        self,
        async_session_factory,
        *,
        preset_metadata: dict | None,
        agent_profile_id: uuid.UUID | None = PROFILE_UUID,
        subject_key: str | None = None,
        tarball_path: str = "https://example.com/code.tar.gz",
    ) -> AutomationRun:
        async with async_session_factory() as session:
            automation = Automation(
                user_id=TEST_USER_ID,
                org_id=TEST_ORG_ID,
                name="Nightly",
                agent_profile_id=agent_profile_id,
                model="Default",
                preset_metadata=preset_metadata,
                trigger={"type": "event", "source": "github-events", "on": "*"},
                tarball_path=tarball_path,
                entrypoint="uv run main.py",
                enabled=True,
            )
            session.add(automation)
            await session.commit()
            run = AutomationRun(
                automation_id=automation.id,
                status=AutomationRunStatus.RUNNING,
                started_at=utcnow(),
                subject_key=subject_key,
            )
            session.add(run)
            await session.commit()
            run_id = run.id
        async with async_session_factory() as session:
            return (
                (
                    await session.execute(
                        select(AutomationRun)
                        .options(selectinload(AutomationRun.automation))
                        .where(AutomationRun.id == run_id)
                    )
                )
                .scalars()
                .one()
            )

    @staticmethod
    def _backend(start_result: Any) -> MagicMock:
        backend = MagicMock()
        backend.is_local_mode = False
        ctx = ExecutionContext(
            agent_url="http://agent.test",
            session_key="sk-1",
            sandbox_id="sbx-1",
            api_url="https://app.test",
            api_key="sk-user",
        )
        backend.get_execution_context = AsyncMock(return_value=ctx)
        backend.release_context = AsyncMock()
        backend.build_env_vars = MagicMock(return_value={})
        backend.get_work_dir = MagicMock(return_value="/workspace/project")
        backend.git_connections = AsyncMock(return_value=CONNECTED)
        if isinstance(start_result, Exception):
            backend.start_conversation = AsyncMock(side_effect=start_result)
        else:
            backend.start_conversation = AsyncMock(return_value=start_result)
        return backend

    async def _reload(self, async_session_factory, run_id) -> AutomationRun:
        async with async_session_factory() as session:
            return (
                (
                    await session.execute(
                        select(AutomationRun).where(AutomationRun.id == run_id)
                    )
                )
                .scalars()
                .one()
            )

    @patch("openhands.automation.dispatcher.execute_in_context", new_callable=AsyncMock)
    async def test_the_runner_is_told_to_attach(
        self, mock_execute, async_session_factory, mock_settings, mock_client
    ):
        run = await self._run(
            async_session_factory,
            preset_metadata={"preset_type": "prompt", "prompt": "hi"},
            subject_key="OpenHands/OpenHands/1",
        )
        backend = self._backend(
            StartedConversation(
                conversation_id=CONVERSATION_ID, agent_profile_id=PROFILE_ID
            )
        )
        mock_execute.return_value = MagicMock(
            success=True, bash_command_id="cmd-1", error=None
        )

        with patch("openhands.automation.dispatcher.get_backend", return_value=backend):
            await _execute_run(run, mock_settings, async_session_factory, mock_client)

        start = backend.start_conversation.await_args.args[2]
        assert start.agent_profile_id == PROFILE_ID
        assert start.llm_profile == "Default"
        derived = conversation_id_for(
            TEST_ORG_ID,
            run.automation_id,
            "github-events",
            "OpenHands/OpenHands/1",
            None,
        )
        assert start.conversation_id == derived
        env_vars = mock_execute.await_args.kwargs["env_vars"]
        assert env_vars["AUTOMATION_CONVERSATION_ID"] == CONVERSATION_ID
        assert env_vars["AUTOMATION_CONVERSATION_STARTED"] == "app_server"
        backend.release_context.assert_not_awaited()

    @patch("openhands.automation.dispatcher.execute_in_context", new_callable=AsyncMock)
    async def test_a_custom_tarball_keeps_creating_its_own_conversation(
        self, mock_execute, async_session_factory, mock_settings, mock_client
    ):
        run = await self._run(async_session_factory, preset_metadata=None)
        backend = self._backend(None)
        mock_execute.return_value = MagicMock(
            success=True, bash_command_id="cmd-1", error=None
        )

        with patch("openhands.automation.dispatcher.get_backend", return_value=backend):
            await _execute_run(run, mock_settings, async_session_factory, mock_client)

        backend.start_conversation.assert_not_awaited()
        env_vars = mock_execute.await_args.kwargs["env_vars"]
        assert "AUTOMATION_CONVERSATION_STARTED" not in env_vars

    @patch("openhands.automation.dispatcher.execute_in_context", new_callable=AsyncMock)
    async def test_an_app_server_without_the_contract_falls_back(
        self, mock_execute, async_session_factory, mock_settings, mock_client, caplog
    ):
        run = await self._run(
            async_session_factory,
            preset_metadata={"preset_type": "prompt", "prompt": "hi"},
            subject_key="OpenHands/OpenHands/1",
        )
        backend = self._backend(
            ConversationStartUnavailable("no route", status_code=404)
        )
        mock_execute.return_value = MagicMock(
            success=True, bash_command_id="cmd-1", error=None
        )

        with patch("openhands.automation.dispatcher.get_backend", return_value=backend):
            await _execute_run(run, mock_settings, async_session_factory, mock_client)

        env_vars = mock_execute.await_args.kwargs["env_vars"]
        assert "AUTOMATION_CONVERSATION_STARTED" not in env_vars
        # The derived id survives, so follow-ups still reach the conversation.
        assert env_vars["AUTOMATION_CONVERSATION_ID"] == conversation_id_for(
            TEST_ORG_ID,
            run.automation_id,
            "github-events",
            "OpenHands/OpenHands/1",
            None,
        )
        backend.release_context.assert_not_awaited()
        assert "conversation_start_fallback" in caplog.text
        reloaded = await self._reload(async_session_factory, run.id)
        assert reloaded.status == AutomationRunStatus.RUNNING

    @patch("openhands.automation.dispatcher.execute_in_context", new_callable=AsyncMock)
    async def test_a_failed_start_fails_the_run_before_any_command(
        self, mock_execute, async_session_factory, mock_settings, mock_client
    ):
        run = await self._run(
            async_session_factory,
            preset_metadata={"preset_type": "prompt", "prompt": "hi"},
        )
        backend = self._backend(
            ConversationStartError(
                "OpenHands API failed to start the conversation: Sandbox not found",
                code="start_task_error",
            )
        )

        with patch("openhands.automation.dispatcher.get_backend", return_value=backend):
            await _execute_run(run, mock_settings, async_session_factory, mock_client)

        mock_execute.assert_not_awaited()
        backend.release_context.assert_awaited_once()
        reloaded = await self._reload(async_session_factory, run.id)
        assert reloaded.status == AutomationRunStatus.FAILED
        assert reloaded.bash_command_id is None
        detail = cast(dict, reloaded.status_detail)
        assert detail["operation"] == "start_conversation"
        assert detail["source"] == "openhands_api"
        assert detail["transient"] is False
        assert detail["code"] == "start_task_error"

    @patch("openhands.automation.dispatcher.execute_in_context", new_callable=AsyncMock)
    async def test_a_transient_failure_is_recorded_as_such(
        self, mock_execute, async_session_factory, mock_settings, mock_client
    ):
        run = await self._run(
            async_session_factory,
            preset_metadata={"preset_type": "prompt", "prompt": "hi"},
        )
        request = httpx.Request("POST", "https://app.test/api/v1/app-conversations")
        backend = self._backend(
            httpx.HTTPStatusError(
                "503", request=request, response=httpx.Response(503, request=request)
            )
        )

        with patch("openhands.automation.dispatcher.get_backend", return_value=backend):
            await _execute_run(run, mock_settings, async_session_factory, mock_client)

        mock_execute.assert_not_awaited()
        reloaded = await self._reload(async_session_factory, run.id)
        assert reloaded.status == AutomationRunStatus.FAILED
        detail = cast(dict, reloaded.status_detail)
        assert detail["operation"] == "start_conversation"
        assert detail["transient"] is True

    @patch("openhands.automation.dispatcher.execute_in_context", new_callable=AsyncMock)
    async def test_the_wrong_profile_fails_the_run(
        self, mock_execute, async_session_factory, mock_settings, mock_client
    ):
        """An app server that falls back to composed settings reports no
        launched profile. The run must not continue as if it had one."""
        run = await self._run(
            async_session_factory,
            preset_metadata={"preset_type": "prompt", "prompt": "hi"},
        )
        backend = self._backend(
            StartedConversation(conversation_id=CONVERSATION_ID, agent_profile_id=None)
        )

        with patch("openhands.automation.dispatcher.get_backend", return_value=backend):
            await _execute_run(run, mock_settings, async_session_factory, mock_client)

        mock_execute.assert_not_awaited()
        backend.release_context.assert_awaited_once()
        reloaded = await self._reload(async_session_factory, run.id)
        assert reloaded.status == AutomationRunStatus.FAILED
        detail = cast(dict, reloaded.status_detail)
        assert detail["code"] == "agent_profile_mismatch"
        assert detail["transient"] is False

    @patch("openhands.automation.dispatcher.execute_in_context", new_callable=AsyncMock)
    async def test_no_requested_profile_accepts_whatever_launched(
        self, mock_execute, async_session_factory, mock_settings, mock_client
    ):
        run = await self._run(
            async_session_factory,
            preset_metadata={"preset_type": "prompt", "prompt": "hi"},
            agent_profile_id=None,
        )
        backend = self._backend(
            StartedConversation(conversation_id=CONVERSATION_ID, agent_profile_id=None)
        )
        mock_execute.return_value = MagicMock(
            success=True, bash_command_id="cmd-1", error=None
        )

        with patch("openhands.automation.dispatcher.get_backend", return_value=backend):
            await _execute_run(run, mock_settings, async_session_factory, mock_client)

        env_vars = mock_execute.await_args.kwargs["env_vars"]
        assert env_vars["AUTOMATION_CONVERSATION_STARTED"] == "app_server"

    @patch("openhands.automation.dispatcher.execute_in_context", new_callable=AsyncMock)
    async def test_the_finish_plugin_is_in_the_sandbox_before_the_start(
        self, mock_execute, uploads, async_session_factory, mock_settings, mock_client
    ):
        run = await self._run(
            async_session_factory,
            preset_metadata={"preset_type": "prompt", "prompt": "hi"},
        )
        backend = self._backend(
            StartedConversation(
                conversation_id=CONVERSATION_ID, agent_profile_id=PROFILE_ID
            )
        )
        order: list[str] = []
        uploads.side_effect = lambda *args: order.append("upload")
        backend.start_conversation.side_effect = lambda *args: (
            order.append("start")
            or StartedConversation(
                conversation_id=CONVERSATION_ID, agent_profile_id=PROFILE_ID
            )
        )
        mock_execute.return_value = MagicMock(
            success=True, bash_command_id="cmd-1", error=None
        )

        with patch("openhands.automation.dispatcher.get_backend", return_value=backend):
            await _execute_run(run, mock_settings, async_session_factory, mock_client)

        assert order == ["upload", "start"]
        agent_url, session_key, files = uploads.await_args.args
        assert (agent_url, session_key) == ("http://agent.test", "sk-1")
        assert files == finish_plugin_files(str(run.id))
        start = backend.start_conversation.await_args.args[2]
        assert start.plugins[0] == {"source": finish_plugin_dir(str(run.id))}

    @patch("openhands.automation.dispatcher.execute_in_context", new_callable=AsyncMock)
    async def test_a_failed_plugin_write_fails_the_run_before_the_start(
        self, mock_execute, uploads, async_session_factory, mock_settings, mock_client
    ):
        run = await self._run(
            async_session_factory,
            preset_metadata={"preset_type": "prompt", "prompt": "hi"},
        )
        backend = self._backend(None)
        uploads.side_effect = httpx.ConnectError("agent server unreachable")

        with patch("openhands.automation.dispatcher.get_backend", return_value=backend):
            await _execute_run(run, mock_settings, async_session_factory, mock_client)

        backend.start_conversation.assert_not_awaited()
        mock_execute.assert_not_awaited()
        backend.release_context.assert_awaited_once()
        reloaded = await self._reload(async_session_factory, run.id)
        assert reloaded.status == AutomationRunStatus.FAILED
        detail = cast(dict, reloaded.status_detail)
        assert detail["code"] == "finish_plugin_upload"
        assert detail["source"] == "agent_server"
        assert detail["transient"] is True

    @patch("openhands.automation.dispatcher.execute_in_context", new_callable=AsyncMock)
    async def test_the_runner_is_told_which_repositories_it_still_clones(
        self, mock_execute, async_session_factory, mock_settings, mock_client
    ):
        repos = [
            {"url": "https://github.com/acme/api"},
            {"url": "git@github.com:acme/ssh.git"},
        ]
        run = await self._run(
            async_session_factory,
            preset_metadata={"preset_type": "prompt", "prompt": "hi", "repos": repos},
        )
        backend = self._backend(
            StartedConversation(
                conversation_id=CONVERSATION_ID, agent_profile_id=PROFILE_ID
            )
        )
        mock_execute.return_value = MagicMock(
            success=True, bash_command_id="cmd-1", error=None
        )

        with (
            patch("openhands.automation.dispatcher.get_backend", return_value=backend),
            patch(
                "openhands.automation.dispatcher._runner_takes_repository_list",
                return_value=True,
            ),
        ):
            await _execute_run(run, mock_settings, async_session_factory, mock_client)

        start = backend.start_conversation.await_args.args[2]
        assert start.repositories == [
            {"repository": "acme/api", "git_provider": "github", "branch": None}
        ]
        env_vars = mock_execute.await_args.kwargs["env_vars"]
        assert json.loads(env_vars["AUTOMATION_RUNNER_REPOS"]) == [
            {"url": "git@github.com:acme/ssh.git"}
        ]

    async def _dispatch_repositories(
        self,
        async_session_factory,
        mock_settings,
        mock_client,
        repos: list[dict[str, Any]],
        connections: Any,
    ) -> tuple[MagicMock, dict[str, str], AutomationRun]:
        """Dispatch a prompt preset whose runner takes a repository list.

        ``connections`` is what reading the owner's git connections returns,
        or the exception it raises.
        """
        run = await self._run(
            async_session_factory,
            preset_metadata={"preset_type": "prompt", "prompt": "hi", "repos": repos},
        )
        backend = self._backend(
            StartedConversation(
                conversation_id=CONVERSATION_ID, agent_profile_id=PROFILE_ID
            )
        )
        if isinstance(connections, BaseException):
            backend.git_connections = AsyncMock(side_effect=connections)
        else:
            backend.git_connections = AsyncMock(return_value=connections)
        with (
            patch("openhands.automation.dispatcher.get_backend", return_value=backend),
            patch(
                "openhands.automation.dispatcher._runner_takes_repository_list",
                return_value=True,
            ),
            patch(
                "openhands.automation.dispatcher.execute_in_context",
                new_callable=AsyncMock,
                return_value=MagicMock(
                    success=True, bash_command_id="cmd-1", error=None
                ),
            ) as mock_execute,
        ):
            await _execute_run(run, mock_settings, async_session_factory, mock_client)
        executed = mock_execute.await_args
        assert executed is not None
        return backend, executed.kwargs["env_vars"], run

    async def test_a_public_repository_on_an_unconnected_provider_stays_with_the_runner(
        self, async_session_factory, mock_settings, mock_client
    ):
        """The app server clones only through the owner's connection to the
        provider. Without one it fails the start; the runner clones a public
        repository anonymously, as it always has."""
        backend, env_vars, _ = await self._dispatch_repositories(
            async_session_factory,
            mock_settings,
            mock_client,
            [{"url": "https://github.com/acme/public"}],
            {"bitbucket_data_center": "bitbucket.example.com"},
        )

        start = backend.start_conversation.await_args.args[2]
        assert start.repositories == []
        assert env_vars["AUTOMATION_CONVERSATION_STARTED"] == "app_server"
        # No list: the runner clones every recorded repository.
        assert "AUTOMATION_RUNNER_REPOS" not in env_vars

    async def test_a_repository_on_a_connected_provider_and_host_goes_to_the_app_server(
        self, async_session_factory, mock_settings, mock_client
    ):
        backend, env_vars, _ = await self._dispatch_repositories(
            async_session_factory,
            mock_settings,
            mock_client,
            [
                {
                    "url": "https://gitlab.example.com/team/api",
                    "provider": "gitlab",
                }
            ],
            {"gitlab": "https://gitlab.example.com"},
        )

        start = backend.start_conversation.await_args.args[2]
        assert start.repositories == [
            {"repository": "team/api", "git_provider": "gitlab", "branch": None}
        ]
        assert json.loads(env_vars["AUTOMATION_RUNNER_REPOS"]) == []

    async def test_the_public_host_of_a_self_managed_connection_stays_with_the_runner(
        self, async_session_factory, mock_settings, mock_client
    ):
        """An owner connected to GitHub Enterprise or a self-managed GitLab:
        the app server would look the name up on that host, not the public one."""
        repos = [
            {"url": "https://github.com/acme/api"},
            {"url": "https://gitlab.com/group/web"},
        ]
        backend, env_vars, _ = await self._dispatch_repositories(
            async_session_factory,
            mock_settings,
            mock_client,
            repos,
            {
                "github": "https://ghe.example.com/api/v3",
                "gitlab": "gitlab.example.com",
            },
        )

        start = backend.start_conversation.await_args.args[2]
        assert start.repositories == []
        assert "AUTOMATION_RUNNER_REPOS" not in env_vars

    async def test_a_mixed_list_is_split_and_every_repository_stays_required(
        self, async_session_factory, mock_settings, mock_client
    ):
        """Connected ones go to the app server, which must clone them all
        (``require_all``); the runner clones the rest and fails the run if one
        cannot be cloned. No run starts with a repository missing."""
        repos = [
            {"url": "https://ghe.example.com/acme/api", "provider": "github"},
            {"url": "https://github.com/acme/public"},
            {"url": "https://gitlab.com/group/web"},
            {"url": "acme/tools", "provider": "github", "ref": "v2"},
            {"url": "git@ghe.example.com:acme/ssh.git"},
        ]
        backend, env_vars, _ = await self._dispatch_repositories(
            async_session_factory,
            mock_settings,
            mock_client,
            repos,
            {"github": "ghe.example.com"},
        )

        start = backend.start_conversation.await_args.args[2]
        assert start.repositories == [
            {"repository": "acme/api", "git_provider": "github", "branch": None},
            {"repository": "acme/tools", "git_provider": "github", "branch": "v2"},
        ]
        assert json.loads(env_vars["AUTOMATION_RUNNER_REPOS"]) == [
            {"url": "https://github.com/acme/public"},
            {"url": "https://gitlab.com/group/web"},
            {"url": "git@ghe.example.com:acme/ssh.git"},
        ]

        # The request the backend builds from that split requires every one.
        server = AppServer()
        async with server.client() as client:
            await _backend().start_conversation(client, _ctx(), start)
        body = server.posted_body()
        assert body["selected_repository"] == "acme/api"
        assert body["additional_repositories"] == [
            {"repository": "acme/tools", "git_provider": "github", "branch": "v2"}
        ]
        assert body["require_all"] is True

    @pytest.mark.parametrize(
        "failure",
        [
            ConversationStartError(
                "OpenHands API refused to read the owner's git connections "
                "(HTTP 500): boom",
                status_code=500,
            ),
            httpx.ReadTimeout("timed out"),
        ],
    )
    async def test_unreadable_connections_leave_every_repository_to_the_runner(
        self, failure, async_session_factory, mock_settings, mock_client, caplog
    ):
        backend, env_vars, run = await self._dispatch_repositories(
            async_session_factory,
            mock_settings,
            mock_client,
            [{"url": "https://github.com/acme/api"}],
            failure,
        )

        start = backend.start_conversation.await_args.args[2]
        assert start.repositories == []
        assert "AUTOMATION_RUNNER_REPOS" not in env_vars
        assert env_vars["AUTOMATION_CONVERSATION_STARTED"] == "app_server"
        assert "git_connections_unavailable" in caplog.text
        reloaded = await self._reload(async_session_factory, run.id)
        assert reloaded.status != AutomationRunStatus.FAILED

    @patch("openhands.automation.dispatcher.execute_in_context", new_callable=AsyncMock)
    async def test_an_older_runner_keeps_cloning_every_repository(
        self, mock_execute, async_session_factory, mock_settings, mock_client
    ):
        run = await self._run(
            async_session_factory,
            preset_metadata={
                "preset_type": "prompt",
                "prompt": "hi",
                "repos": [{"url": "https://github.com/acme/api"}],
            },
        )
        backend = self._backend(
            StartedConversation(
                conversation_id=CONVERSATION_ID, agent_profile_id=PROFILE_ID
            )
        )
        mock_execute.return_value = MagicMock(
            success=True, bash_command_id="cmd-1", error=None
        )

        # This run's tarball is a download, not a preset tarball the service
        # can read, so its runner gets no repository list.
        with patch("openhands.automation.dispatcher.get_backend", return_value=backend):
            await _execute_run(run, mock_settings, async_session_factory, mock_client)

        start = backend.start_conversation.await_args.args[2]
        assert start.repositories == []
        env_vars = mock_execute.await_args.kwargs["env_vars"]
        assert env_vars["AUTOMATION_CONVERSATION_STARTED"] == "app_server"
        assert "AUTOMATION_RUNNER_REPOS" not in env_vars

    @patch("openhands.automation.dispatcher.execute_in_context", new_callable=AsyncMock)
    async def test_a_preset_created_on_1_27_0_runs_with_the_current_runner(
        self, mock_execute, uploads, async_session_factory, mock_settings, mock_client
    ):
        """An automation created on an earlier release needs no action: its
        stored runner is replaced at dispatch, so the run gets the repository
        hand-off and the finish requirement like a new one."""
        repos = [
            {"url": "https://github.com/acme/api"},
            {"url": "git@github.com:acme/ssh.git"},
        ]
        run = await self._run(
            async_session_factory,
            preset_metadata={"preset_type": "prompt", "prompt": "hi", "repos": repos},
            tarball_path=build_internal_url(uuid.uuid4()),
        )
        stored = (FIXTURES / "prompt-1.27.0.tar.gz").read_bytes()
        backend = self._backend(
            StartedConversation(
                conversation_id=CONVERSATION_ID, agent_profile_id=PROFILE_ID
            )
        )
        mock_execute.return_value = MagicMock(
            success=True, bash_command_id="cmd-1", error=None
        )

        with (
            patch("openhands.automation.dispatcher.get_backend", return_value=backend),
            patch(
                "openhands.automation.dispatcher._download_internal_tarball",
                new_callable=AsyncMock,
                return_value=stored,
            ),
        ):
            await _execute_run(run, mock_settings, async_session_factory, mock_client)

        tarball = mock_execute.await_args.kwargs["tarball_source"]
        with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
            member = tar.extractfile("main.py")
            assert member is not None
            main = member.read().decode()
        assert main == (PRESETS_DIR / "prompt" / "sdk_main.py").read_text()
        start = backend.start_conversation.await_args.args[2]
        assert start.repositories == [
            {"repository": "acme/api", "git_provider": "github", "branch": None}
        ]
        assert start.plugins[0] == {"source": finish_plugin_dir(str(run.id))}
        uploads.assert_awaited_once()
        env_vars = mock_execute.await_args.kwargs["env_vars"]
        assert env_vars["AUTOMATION_CONVERSATION_STARTED"] == "app_server"
        assert json.loads(env_vars["AUTOMATION_RUNNER_REPOS"]) == [
            {"url": "git@github.com:acme/ssh.git"}
        ]

    @pytest.mark.parametrize(
        ("fixture", "preset_metadata"),
        [
            ("prompt-1.27.0.tar.gz", {"preset_type": "prompt", "prompt": "hi"}),
            (
                "plugin-1.27.0.tar.gz",
                {
                    "preset_type": "plugin",
                    "prompt": "hi",
                    "plugins": [{"source": "github:acme/plugins"}],
                },
            ),
        ],
    )
    @patch("openhands.automation.dispatcher.execute_in_context", new_callable=AsyncMock)
    async def test_a_cloud_run_needs_no_agent_server_url(
        self,
        mock_execute,
        fixture,
        preset_metadata,
        async_session_factory,
        mock_settings,
        mock_client,
    ):
        """Cloud sandboxes do not receive AGENT_SERVER_URL. The runner a 1.27.0
        preset gets at dispatch takes its mode from AUTOMATION_RUN_MODE, so it
        runs in cloud mode, attached to the started conversation, and would
        even if the variable were set."""
        run = await self._run(
            async_session_factory,
            preset_metadata=preset_metadata,
            tarball_path=build_internal_url(uuid.uuid4()),
        )
        backend = self._backend(
            StartedConversation(
                conversation_id=CONVERSATION_ID, agent_profile_id=PROFILE_ID
            )
        )
        # What CloudSandboxBackend.build_env_vars exports.
        backend.build_env_vars = MagicMock(
            return_value={
                "OPENHANDS_API_KEY": "sk-user",
                "OPENHANDS_CLOUD_API_URL": "https://app.test",
                "AUTOMATION_RUN_MODE": "cloud",
            }
        )
        mock_execute.return_value = MagicMock(
            success=True, bash_command_id="cmd-1", error=None
        )

        with (
            patch("openhands.automation.dispatcher.get_backend", return_value=backend),
            patch(
                "openhands.automation.dispatcher._download_internal_tarball",
                new_callable=AsyncMock,
                return_value=(FIXTURES / fixture).read_bytes(),
            ),
        ):
            await _execute_run(run, mock_settings, async_session_factory, mock_client)

        tarball = mock_execute.await_args.kwargs["tarball_source"]
        with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
            member = tar.extractfile("main.py")
            assert member is not None
            main = member.read().decode()
        detect = _function_from_source(main, "_detect_execution_mode", {})
        env_vars = mock_execute.await_args.kwargs["env_vars"]
        assert "AGENT_SERVER_URL" not in env_vars
        cloud_env = dict(env_vars)
        assert detect(cloud_env) == ("cloud", "AUTOMATION_RUN_MODE")
        assert detect({**cloud_env, "AGENT_SERVER_URL": "http://agent.test"}) == (
            "cloud",
            "AUTOMATION_RUN_MODE",
        )
        # Everything the runner's cloud mode requires is there without it.
        for name in (
            "OPENHANDS_API_KEY",
            "OPENHANDS_CLOUD_API_URL",
            "SANDBOX_ID",
            "SESSION_API_KEY",
        ):
            assert cloud_env.get(name), name
        attaches = _function_from_source(main, "_attaches_to_started_conversation", {})
        assert attaches(cloud_env)


def _function_from_source(
    source: str, function_name: str, namespace: dict, filename: str = "main.py"
):
    module = ast.parse(source, filename=filename)
    function_node = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == function_name
    )
    ast.fix_missing_locations(function_node)
    exec(
        compile(ast.Module(body=[function_node], type_ignores=[]), filename, "exec"),
        namespace,
    )
    return namespace[function_name]


def _load_preset_function(preset_name: str, function_name: str, namespace: dict):
    source_path = PRESETS_DIR / preset_name / "sdk_main.py"
    return _function_from_source(
        source_path.read_text(), function_name, namespace, str(source_path)
    )


PRESETS = ["prompt", "plugin", "plugin_spec"]


class TestRunnersAttachToTheStartedConversation:
    @pytest.mark.parametrize("preset_name", PRESETS)
    def test_attaches_only_when_the_service_started_it(self, preset_name):
        attaches = _load_preset_function(
            preset_name, "_attaches_to_started_conversation", {}
        )
        assert attaches(
            {
                "AUTOMATION_CONVERSATION_ID": CONVERSATION_ID,
                "AUTOMATION_CONVERSATION_STARTED": "app_server",
            }
        )
        # A derived id alone is the continue_conversation contract: the runner
        # still creates the conversation under it.
        assert not attaches({"AUTOMATION_CONVERSATION_ID": CONVERSATION_ID})
        assert not attaches({"AUTOMATION_CONVERSATION_STARTED": "app_server"})
        assert not attaches({})

    @pytest.mark.parametrize("preset_name", PRESETS)
    def test_the_attached_conversation_is_the_runs_conversation(self, preset_name):
        """``attach`` does not register the conversation with the workspace as
        ``create`` does. Without that the completion callback carries no
        conversation id, so the run loses its conversation and finish result."""
        attached = SimpleNamespace(id=uuid.UUID(CONVERSATION_ID))
        remote = MagicMock()
        remote.attach.return_value = attached
        attach = _load_preset_function(
            preset_name,
            "_attach_started_conversation",
            {"RemoteConversation": remote, "uuid": uuid},
        )
        workspace = MagicMock()
        callbacks = [lambda event: None]

        assert attach(workspace, CONVERSATION_ID, callbacks=callbacks) is attached
        remote.attach.assert_called_once_with(
            workspace, uuid.UUID(CONVERSATION_ID), callbacks=callbacks
        )
        workspace.register_conversation.assert_called_once_with(CONVERSATION_ID)

        # A workspace that cannot register one is attached all the same.
        assert attach(SimpleNamespace(), CONVERSATION_ID) is attached

    @pytest.mark.parametrize("preset_name", PRESETS)
    def test_source_attaches_instead_of_creating(self, preset_name):
        source = (PRESETS_DIR / preset_name / "sdk_main.py").read_text()
        assert "RemoteConversation.attach(" in source

    @pytest.mark.parametrize("preset_name", ["prompt", "plugin"])
    def test_repository_instructions_ride_in_the_task_message(self, preset_name):
        """The app server built the system prompt before the runner cloned the
        repositories, so their always-on instructions go into the task text."""
        section = _load_preset_function(preset_name, "_repo_instructions_section", {})
        skills = [
            SimpleNamespace(
                name="repo",
                content="Run make test.",
                trigger=None,
                is_agentskills_format=False,
            ),
            SimpleNamespace(
                name="deploy",
                content="never",
                trigger=object(),
                is_agentskills_format=False,
            ),
            SimpleNamespace(
                name="skillmd",
                content="never",
                trigger=None,
                is_agentskills_format=True,
            ),
        ]
        text = section(skills)
        assert text is not None
        assert text.startswith("## Repository instructions")
        assert "Run make test." in text
        assert "never" not in text
        assert section([]) is None
        assert section([skills[1]]) is None

    @pytest.mark.parametrize("preset_name", ["prompt", "plugin"])
    def test_reads_the_list_only_when_attaching(self, preset_name):
        listed = _load_preset_function(
            preset_name, "_listed_runner_repos", {"json": json}
        )
        left = [{"url": "git@github.com:acme/ssh.git"}]
        env = {"AUTOMATION_RUNNER_REPOS": json.dumps(left)}

        assert listed(env, True) == left
        assert listed({"AUTOMATION_RUNNER_REPOS": "[]"}, True) == []
        # Without the list, or when this script creates the conversation itself,
        # it clones every recorded repository as before.
        assert listed({}, True) is None
        assert listed(env, False) is None

    @pytest.mark.parametrize("preset_name", ["prompt", "plugin"])
    def test_clones_only_what_the_app_server_left(self, preset_name, tmp_path):
        clone = _load_preset_function(
            preset_name,
            "_clone_configured_repos",
            {"json": json, "os": __import__("os"), "report_phase": lambda _: None},
        )
        config = tmp_path / "repos_config.json"
        config.write_text(
            json.dumps(
                [
                    {"url": "https://github.com/acme/api"},
                    {"url": "git@github.com:acme/ssh.git"},
                ]
            )
        )
        workspace = MagicMock()
        workspace.clone_repos.return_value = MagicMock(success_count=1, failed_repos=[])
        left = [{"url": "git@github.com:acme/ssh.git"}]

        assert clone(workspace, str(config), left) is workspace.clone_repos.return_value
        workspace.clone_repos.assert_called_once_with(left)

        workspace.clone_repos.reset_mock()
        assert clone(workspace, str(config), []) is None
        workspace.clone_repos.assert_not_called()

        # A repository left to the runner is as required as before.
        workspace.clone_repos.return_value = MagicMock(
            success_count=0, failed_repos=["git@github.com:acme/ssh.git"]
        )
        with pytest.raises(RuntimeError, match="acme/ssh"):
            clone(workspace, str(config), left)
