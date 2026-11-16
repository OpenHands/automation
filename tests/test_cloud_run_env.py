"""What a run's own script sees: cloud runs never get AGENT_SERVER_URL.

Scripts stored in automations decide "local Agent Canvas" from AGENT_SERVER_URL
being set: the preset runners released up to 1.27.0, the OpenHands/extensions
catalog bundles and the scripts agents write from the openhands-automation
skill. Automations keep the files they were created with, so a cloud run that
exported the URL would send all of them to the sandbox's own agent-server for
their LLM, secrets and conversations, and they would fail (#688). These tests
run those checks, copied from the released code (see the fixture READMEs),
against the environment the dispatcher and ``run_automation()`` hand a run.
"""

import ast
import contextlib
import json
import os
import shlex
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from openhands.automation.backends import (
    CloudSandboxBackend,
    ConversationStart,
    ExecutionContext,
    LocalAgentServerBackend,
    StartedConversation,
)
from openhands.automation.dispatcher import _execute_run
from openhands.automation.exceptions import ConversationStartUnavailable
from openhands.automation.execution import build_tarball, run_automation
from openhands.automation.git_sync.serializer import canonical_tarball_bytes
from openhands.automation.models import (
    Automation,
    AutomationRun,
    AutomationRunStatus,
    TarballUpload,
    UploadStatus,
)
from openhands.automation.plugin_specs import build_execution_payload
from openhands.automation.preset_router import (
    _generate_plugin_tarball,
    _generate_tarball,
    _replace_files_in_tarball,
)
from openhands.automation.utils import utcnow
from openhands.automation.utils.tarball_validation import build_internal_url
from openhands.sdk.plugin import PluginSource
from tests.test_plugin_specs import write_conversation_plugin, write_plugin


FIXTURES = Path(__file__).parent / "fixtures"
RELEASED_PRESETS = FIXTURES / "released_presets"
EXTENSION_SCRIPTS = FIXTURES / "extension_scripts"
PRESETS_DIR = Path(__file__).parent.parent / "openhands" / "automation" / "presets"

USER_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
ORG_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")
AGENT_URL = "http://agent.test"
LOCAL_AGENT_URL = "http://localhost:3000"
API_URL = "https://app.test"
STORED_FORMS = ["as-created", "prompt-edited", "git-synced"]

# A raw script automation, the way catalog bundles and skill-written scripts are
# uploaded (no prompt.txt).
SCRIPT_FILES: dict[str, str | bytes] = {
    "main.py": "from github_client import is_cloud_run\nprint(is_cloud_run())\n",
    "github_client.py": "import os\n",
}


@pytest.fixture
def mock_client():
    return MagicMock()


# -- What the released scripts decide --------------------------------------------


@contextlib.contextmanager
def _environ(env: dict[str, str]):
    with patch.dict(os.environ, env, clear=True):
        yield


def _exec_fixture(name: str, env: dict[str, str]) -> dict[str, Any]:
    """Run one copied extensions file in ``env``; return its namespace."""
    namespace: dict[str, Any] = {}
    source = (EXTENSION_SCRIPTS / name).read_text()
    with _environ(env):
        exec(compile(source, name, "exec"), namespace)
    return namespace


def _bundle_modes(env: dict[str, str]) -> dict[str, str]:
    """What each extensions check concludes in ``env``: "cloud" or "local"."""
    is_cloud_run = _exec_fixture("github_client_is_cloud_run.py.txt", env)
    with _environ(env):
        github_client = is_cloud_run["is_cloud_run"]()
    flag = _exec_fixture("is_cloud_flag.py.txt", env)["IS_CLOUD"]
    jira = not _exec_fixture("is_local_flag.py.txt", env)["IS_LOCAL"]
    dispatcher_cls = _exec_fixture("agent_conversation_dispatcher.py.txt", env)[
        "AgentConversationDispatcher"
    ]
    with _environ(env):
        conversations = dispatcher_cls()._cloud is not None
    return {
        check: "cloud" if cloud else "local"
        for check, cloud in {
            "github_client.is_cloud_run()": github_client,
            "IS_CLOUD": flag,
            "IS_LOCAL": jira,
            "AgentConversationDispatcher": conversations,
        }.items()
    }


def _skill_script_requests(env: dict[str, str]) -> dict[str, Any]:
    """The URLs and callback key a script written from the skill uses in ``env``."""
    helpers = _exec_fixture("skill_custom_script_helpers.py.txt", env)
    requests: list[Any] = []

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b"secret-value"

    def _urlopen(request, *args, **kwargs):
        requests.append(request)
        return _Response()

    with _environ(env), patch.object(helpers["urllib"].request, "urlopen", _urlopen):
        value = helpers["get_secret"]("GITHUB_TOKEN")
        helpers["fire_callback"]("COMPLETED")
    secret, callback = requests
    return {
        "value": value,
        "secret_url": secret.full_url,
        "callback_authorization": callback.get_header("Authorization"),
    }


def _runner_mode(runner: str, environ: dict[str, str]) -> str:
    """Run a preset runner's own top-level statements, up to its
    ``IS_LOCAL_MODE`` assignment, in ``environ``; return the mode it picks."""
    module = ast.parse(runner)
    statements: list[ast.stmt] = []
    for node in module.body:
        statements.append(node)
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "IS_LOCAL_MODE"
            for target in node.targets
        ):
            break
    else:
        raise AssertionError("the runner never assigns IS_LOCAL_MODE")
    namespace: dict[str, Any] = {}
    code = compile(ast.Module(body=statements, type_ignores=[]), "main.py", "exec")
    with _environ(environ):
        exec(code, namespace)
    return "local" if namespace["IS_LOCAL_MODE"] else "cloud"


def _released_runner(kind: str) -> str:
    return (RELEASED_PRESETS / f"{kind}_sdk_main_1.27.0.py.txt").read_text()


def _current_runner(kind: str) -> str:
    return (PRESETS_DIR / kind / "sdk_main.py").read_text()


def _preset_tarball(kind: str, runner: str | None = None) -> bytes:
    """A preset tarball from today's endpoints, optionally with another runner.

    Only ``main.py`` changed in the layout since 1.27.0, so swapping in the
    released runner gives the tarball a 1.27.0 automation still has stored.
    """
    if kind == "prompt":
        tarball = _generate_tarball("Summarize the new issue")
    else:
        tarball = _generate_plugin_tarball(
            [PluginSource(source="github:owner/repo")], "Summarize the new issue"
        )
    if runner is None:
        return tarball
    stored = _replace_files_in_tarball(tarball, {"main.py": runner})
    assert stored is not None
    return stored


def _stored_preset(kind: str, form: str) -> bytes:
    """A preset created on 1.27.0, in a form it can be dispatched in later."""
    tarball = _preset_tarball(kind, _released_runner(kind))
    if form == "prompt-edited":
        edited = _replace_files_in_tarball(tarball, {"prompt.txt": "Edited later"})
        assert edited is not None
        return edited
    if form == "git-synced":
        return canonical_tarball_bytes(tarball)
    return tarball


# -- Dispatching a run -----------------------------------------------------------


async def _make_run(
    async_session_factory,
    tarball: bytes | str,
    preset_metadata: dict[str, Any] | None = None,
) -> AutomationRun:
    """A running run of an automation whose code is ``tarball`` (bytes are
    stored as an internal upload, a string is an external URL)."""
    async with async_session_factory() as session:
        if isinstance(tarball, bytes):
            upload_id = uuid.uuid4()
            session.add(
                TarballUpload(
                    id=upload_id,
                    user_id=USER_ID,
                    org_id=ORG_ID,
                    name="stored-tarball",
                    status=UploadStatus.COMPLETED,
                    storage_path=f"uploads/{upload_id}.tar.gz",
                )
            )
            tarball_path = build_internal_url(upload_id)
        else:
            tarball_path = tarball
        automation = Automation(
            user_id=USER_ID,
            org_id=ORG_ID,
            name="Stored tarball",
            trigger={"type": "cron", "schedule": "* * * * *", "timezone": "UTC"},
            tarball_path=tarball_path,
            entrypoint="python3 main.py",
            enabled=True,
            preset_metadata=preset_metadata,
        )
        session.add(automation)
        await session.commit()
        run = AutomationRun(
            automation_id=automation.id,
            status=AutomationRunStatus.RUNNING,
            started_at=utcnow(),
        )
        session.add(run)
        await session.commit()
        run_id = run.id

    async with async_session_factory() as session:
        return (
            await session.execute(
                select(AutomationRun)
                .options(selectinload(AutomationRun.automation))
                .where(AutomationRun.id == run_id)
            )
        ).scalar_one()


def _cloud_backend(run: AutomationRun) -> tuple[CloudSandboxBackend, ExecutionContext]:
    backend = CloudSandboxBackend(api_url=API_URL, run=run)
    ctx = ExecutionContext(
        agent_url=AGENT_URL,
        session_key="sk-1",
        sandbox_id="sbx-1",
        api_url=API_URL,
        api_key="cloud-api-key",
    )
    return backend, ctx


def _local_backend(run: AutomationRun) -> tuple[LocalAgentServerBackend, Any]:
    backend = LocalAgentServerBackend(
        agent_server_url=LOCAL_AGENT_URL,
        api_key="local-session-key",
        run=run,
        workspace_base="/workspace",
        callback_api_key="local-callback-key",
    )
    return backend, None


async def _dispatch(
    async_session_factory,
    mock_settings,
    mock_client,
    tarball: bytes | str,
    *,
    mode: str = "cloud",
    plugin_payload: tuple | None = None,
    preset_metadata: dict[str, Any] | None = None,
    git_connections: dict[str, str | None] | None = None,
    app_server_starts: bool = True,
) -> dict[str, str]:
    """Dispatch a run through ``_execute_run``; return the env the sandbox gets.

    Stock runners attach to a conversation the app server starts for them;
    ``app_server_starts=False`` is an app server without that route, where the
    runner creates the conversation itself.
    """
    run = await _make_run(async_session_factory, tarball, preset_metadata)
    if plugin_payload is not None:
        run.automation.plugin_spec = {"plugin": {"source": "github:org/plugin"}}
        run.automation.tarball_path = "plugin-spec://runtime"
    backend, ctx = (_cloud_backend if mode == "cloud" else _local_backend)(run)
    store = MagicMock()
    store.read.return_value = tarball
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch(
                "openhands.automation.backends.cloud.get_api_key_for_automation_run",
                new_callable=AsyncMock,
                return_value="cloud-api-key",
            )
        )
        if ctx is not None:
            stack.enter_context(
                patch.object(
                    backend, "get_execution_context", AsyncMock(return_value=ctx)
                )
            )

            # A stock runner attaches to the conversation the app server starts
            # for it, from the profile the run asked for.
            def _started(
                _client, _ctx, start: ConversationStart
            ) -> StartedConversation:
                if not app_server_starts:
                    raise ConversationStartUnavailable("no such route", status_code=404)
                return StartedConversation(
                    conversation_id=str(uuid.uuid4()),
                    agent_profile_id=start.agent_profile_id,
                )

            stack.enter_context(
                patch.object(
                    backend, "start_conversation", AsyncMock(side_effect=_started)
                )
            )
            stack.enter_context(
                patch.object(
                    backend,
                    "git_connections",
                    AsyncMock(return_value=git_connections or {}),
                )
            )
        stack.enter_context(
            patch("openhands.automation.dispatcher.get_backend", return_value=backend)
        )
        stack.enter_context(
            patch("openhands.automation.storage.get_file_store", return_value=store)
        )
        if plugin_payload is not None:
            stack.enter_context(
                patch(
                    "openhands.automation.dispatcher.build_execution_payload",
                    new_callable=AsyncMock,
                    return_value=plugin_payload,
                )
            )
            stack.enter_context(
                patch(
                    "openhands.automation.dispatcher._validate_plugin_run_agent_profile",
                    new_callable=AsyncMock,
                    return_value=(None, None),
                )
            )
            stack.enter_context(
                patch(
                    "openhands.automation.dispatcher._fetch_plugins_on_agent_server",
                    new_callable=AsyncMock,
                    return_value=[],
                )
            )
        # The real execute_in_context writes the env file the run sources; only
        # the transfers into the sandbox are stubbed.
        mock_upload = stack.enter_context(
            patch("openhands.automation.execution._upload", new_callable=AsyncMock)
        )
        stack.enter_context(
            patch(
                "openhands.automation.execution._download_in_sandbox",
                new_callable=AsyncMock,
            )
        )
        mock_start = stack.enter_context(
            patch(
                "openhands.automation.execution._start_bash",
                new_callable=AsyncMock,
                return_value="cmd-1",
            )
        )
        if mode == "cloud":
            await backend.get_api_key()
        await _execute_run(run, mock_settings, async_session_factory, mock_client)

    assert mock_start.await_args is not None, "the run was not dispatched"
    env_files = [
        call.args[1]
        for call in mock_upload.await_args_list
        if str(call.args[2]).endswith(".env")
    ]
    assert len(env_files) == 1, "the run's env file was not uploaded"
    return _exported_env(env_files[0])


async def _plugin_payload(tmp_path: Path, write) -> tuple:
    return await build_execution_payload(
        plugin_spec={"plugin": {"source": str(write(tmp_path))}, "config": {}},
        agent_profile_id=None,
        agent_profile_name=None,
        llm_profile_name=None,
        work_dir="/workspace/project",
    )


@contextlib.contextmanager
def _blocking_cloud_run():
    """Stub the sandbox for ``run_automation()``; yields the upload mock."""
    with (
        patch("openhands.automation.execution.httpx.AsyncClient") as mock_client,
        patch(
            "openhands.automation.execution._create_and_wait",
            new_callable=AsyncMock,
            return_value=("sbx-1", "sk-1", AGENT_URL),
        ),
        patch(
            "openhands.automation.execution._upload", new_callable=AsyncMock
        ) as mock_upload,
        patch(
            "openhands.automation.execution._bash",
            new_callable=AsyncMock,
            return_value=(0, "", ""),
        ),
    ):
        context_manager = MagicMock()
        context_manager.__aenter__ = AsyncMock(return_value=AsyncMock())
        context_manager.__aexit__ = AsyncMock(return_value=None)
        mock_client.return_value = context_manager
        yield mock_upload


def _exported_env(env_file: bytes) -> dict[str, str]:
    """Parse the ``export NAME='value'`` file uploaded next to the tarball."""
    exports = (shlex.split(line)[1] for line in env_file.decode().splitlines())
    return dict(item.split("=", 1) for item in exports)


async def _blocking_run_env(tarball: bytes) -> dict[str, str]:
    with _blocking_cloud_run() as mock_upload:
        await run_automation(
            api_url=API_URL,
            api_key="cloud-api-key",
            entrypoint="python3 main.py",
            tarball_source=tarball,
            keep_sandbox=True,
        )
    return _exported_env(mock_upload.await_args_list[1].args[1])


# -- Cloud runs take the cloud path ---------------------------------------------


class TestCloudRunsTakeTheCloudPath:
    """Every stored script decides "cloud" in a cloud run, and the run carries
    no AGENT_SERVER_URL for anything to need."""

    @pytest.mark.parametrize("kind", ["prompt", "plugin"])
    @pytest.mark.parametrize("form", STORED_FORMS)
    async def test_preset_created_on_1_27_0(
        self, kind, form, async_session_factory, mock_settings, mock_client
    ):
        env = await _dispatch(
            async_session_factory,
            mock_settings,
            mock_client,
            _stored_preset(kind, form),
        )

        assert _runner_mode(_released_runner(kind), env) == "cloud"
        assert "AGENT_SERVER_URL" not in env
        assert env["SANDBOX_ID"] == "sbx-1"
        assert env["SESSION_API_KEY"] == "sk-1"
        assert env["OPENHANDS_API_KEY"] == "cloud-api-key"

    @pytest.mark.parametrize("kind", ["prompt", "plugin"])
    async def test_new_preset(
        self, kind, async_session_factory, mock_settings, mock_client
    ):
        env = await _dispatch(
            async_session_factory, mock_settings, mock_client, _preset_tarball(kind)
        )

        assert "AGENT_SERVER_URL" not in env
        assert env["AUTOMATION_RUN_MODE"] == "cloud"
        assert _runner_mode(_current_runner(kind), env) == "cloud"

    async def test_catalog_bundle_checks(
        self, async_session_factory, mock_settings, mock_client
    ):
        env = await _dispatch(
            async_session_factory,
            mock_settings,
            mock_client,
            build_tarball(SCRIPT_FILES),
        )

        assert _bundle_modes(env) == {
            "github_client.is_cloud_run()": "cloud",
            "IS_CLOUD": "cloud",
            "IS_LOCAL": "cloud",
            "AgentConversationDispatcher": "cloud",
        }
        assert "AGENT_SERVER_URL" not in env

    async def test_script_written_from_the_skill(
        self, async_session_factory, mock_settings, mock_client
    ):
        env = await _dispatch(
            async_session_factory,
            mock_settings,
            mock_client,
            build_tarball(SCRIPT_FILES),
        )

        requests = _skill_script_requests(env)

        assert requests["secret_url"] == (
            f"{API_URL}/api/v1/sandboxes/sbx-1/settings/secrets/GITHUB_TOKEN"
        )
        assert requests["callback_authorization"] == "Bearer cloud-api-key"

    async def test_run_automation(self):
        env = await _blocking_run_env(_stored_preset("prompt", "as-created"))

        assert _runner_mode(_released_runner("prompt"), env) == "cloud"
        assert "AGENT_SERVER_URL" not in env
        assert env["AUTOMATION_RUN_MODE"] == "cloud"
        assert env["AUTOMATION_CALLBACK_API_KEY"] == "cloud-api-key"


def _uses(source: str, match) -> list[tuple[ast.AST, list[ast.AST]]]:
    """Every node of ``source`` that ``match`` accepts, with its ancestors."""
    found: list[tuple[ast.AST, list[ast.AST]]] = []

    def visit(node: ast.AST, parents: list[ast.AST]) -> None:
        if match(node):
            found.append((node, parents))
        for child in ast.iter_child_nodes(node):
            visit(child, [*parents, node])

    visit(ast.parse(source), [])
    return found


def _names_the_variable(node: ast.AST) -> bool:
    """``"AGENT_SERVER_URL"`` itself, as in ``os.environ["AGENT_SERVER_URL"]``,
    ``os.environ.get(...)`` or ``os.getenv(...)``."""
    return isinstance(node, ast.Constant) and node.value == "AGENT_SERVER_URL"


class TestCurrentRunnersUseTheUrlOnlyLocally:
    """Nothing in today's runners needs AGENT_SERVER_URL in a cloud sandbox:
    they read it only on their local-mode branch."""

    @staticmethod
    def _local_only(node: ast.AST, parents: list[ast.AST]) -> bool:
        return any(
            isinstance(parent, ast.If)
            and isinstance(parent.test, ast.Name)
            and parent.test.id == "IS_LOCAL_MODE"
            and any(node is inner for stmt in parent.body for inner in ast.walk(stmt))
            for parent in parents
        )

    @staticmethod
    def _allowed_read(node: ast.AST, parents: list[ast.AST]) -> bool:
        """Where a runner may name the variable: the ``agent_server_url``
        assignment, the mode fallback for services that predate
        AUTOMATION_RUN_MODE, and the local-mode branch."""
        return (
            any(
                isinstance(parent, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == "agent_server_url"
                    for target in parent.targets
                )
                for parent in parents
            )
            or any(
                isinstance(parent, ast.FunctionDef)
                and parent.name == "_detect_execution_mode"
                for parent in parents
            )
            or TestCurrentRunnersUseTheUrlOnlyLocally._local_only(node, parents)
        )

    @pytest.mark.parametrize("kind", ["prompt", "plugin", "plugin_spec"])
    def test_agent_server_url_is_used_only_in_local_mode(self, kind):
        uses = _uses(
            _current_runner(kind),
            lambda node: (
                isinstance(node, ast.Name)
                and node.id == "agent_server_url"
                and isinstance(node.ctx, ast.Load)
            ),
        )

        assert uses, "the runner no longer reads agent_server_url"
        assert all(self._local_only(node, parents) for node, parents in uses)

    @pytest.mark.parametrize("kind", ["prompt", "plugin", "plugin_spec"])
    def test_the_variable_is_read_only_where_allowed(self, kind):
        reads = _uses(_current_runner(kind), _names_the_variable)

        assert reads, "the runner no longer reads AGENT_SERVER_URL"
        assert all(self._allowed_read(node, parents) for node, parents in reads)


@pytest.mark.parametrize(
    "module",
    ["dispatcher.py", "execution.py", "backends/cloud.py"],
)
def test_cloud_env_code_never_names_the_variable(module):
    """Guard: the code that builds a cloud run's environment never writes
    AGENT_SERVER_URL (comments and docstrings aside)."""
    source = (PRESETS_DIR.parent / module).read_text()

    assert not _uses(source, _names_the_variable)


# -- Local mode is unchanged ------------------------------------------------------


class TestLocalRunsKeepTheUrl:
    """On a local Agent Canvas the run still gets AGENT_SERVER_URL, and every
    stored script takes its local branch."""

    @pytest.mark.parametrize(
        "tarball",
        [
            pytest.param(lambda: build_tarball(SCRIPT_FILES), id="script"),
            pytest.param(lambda: _stored_preset("prompt", "as-created"), id="preset"),
        ],
    )
    async def test_local_run(
        self, tarball, async_session_factory, mock_settings, mock_client
    ):
        env = await _dispatch(
            async_session_factory, mock_settings, mock_client, tarball(), mode="local"
        )

        assert env["AGENT_SERVER_URL"] == LOCAL_AGENT_URL
        assert env["AUTOMATION_RUN_MODE"] == "local"
        assert env["AUTOMATION_CALLBACK_API_KEY"] == "local-callback-key"
        assert "SANDBOX_ID" not in env
        assert _runner_mode(_released_runner("prompt"), env) == "local"
        assert _runner_mode(_current_runner("prompt"), env) == "local"
        assert set(_bundle_modes(env).values()) == {"local"}
        assert _skill_script_requests(env)["secret_url"] == (
            f"{LOCAL_AGENT_URL}/api/settings/secrets/GITHUB_TOKEN"
        )


# -- Guard -------------------------------------------------------------------------


class TestCloudSandboxesNeverGetAgentServerUrl:
    """Guard: no cloud path may export AGENT_SERVER_URL into a sandbox."""

    @pytest.mark.parametrize(
        "tarball",
        [
            pytest.param(lambda: _stored_preset("prompt", "as-created"), id="prompt"),
            pytest.param(lambda: _stored_preset("plugin", "as-created"), id="plugin"),
            pytest.param(lambda: _preset_tarball("prompt"), id="new-prompt"),
            pytest.param(lambda: build_tarball(SCRIPT_FILES), id="script"),
            pytest.param(
                lambda: build_tarball({"main.py": "", "docs/prompt.txt": "x"}),
                id="nested-prompt-txt",
            ),
            pytest.param(lambda: "https://example.com/code.tar.gz", id="url"),
        ],
    )
    async def test_dispatcher(
        self, tarball, async_session_factory, mock_settings, mock_client
    ):
        env = await _dispatch(
            async_session_factory, mock_settings, mock_client, tarball()
        )

        assert "AGENT_SERVER_URL" not in env
        assert env["AUTOMATION_RUN_MODE"] == "cloud"
        assert env["AUTOMATION_CALLBACK_API_KEY"] == "cloud-api-key"

    @pytest.mark.parametrize(
        "write",
        [write_plugin, write_conversation_plugin],
        ids=["script-runner", "conversation-runner"],
    )
    async def test_plugin_spec_runs(
        self, write, tmp_path, async_session_factory, mock_settings, mock_client
    ):
        payload = await _plugin_payload(tmp_path, write)

        env = await _dispatch(
            async_session_factory,
            mock_settings,
            mock_client,
            payload[0],
            plugin_payload=payload,
        )

        assert "AGENT_SERVER_URL" not in env
        assert env["AUTOMATION_EXECUTION_SPEC_PATH"].endswith("execution.json")

    @pytest.mark.parametrize(
        "app_server_starts", [True, False], ids=["started", "fallback"]
    )
    async def test_preset_runs_with_repositories(
        self, app_server_starts, async_session_factory, mock_settings, mock_client
    ):
        """The app-server start, with a repository left to the runner, and the
        fallback where the runner creates the conversation: neither exports it."""
        repos = [
            {"url": "https://gitlab.example.com/team/api", "provider": "gitlab"},
            {"url": "https://github.com/owner/public"},
        ]
        env = await _dispatch(
            async_session_factory,
            mock_settings,
            mock_client,
            _preset_tarball("prompt"),
            preset_metadata={"preset_type": "prompt", "prompt": "x", "repos": repos},
            git_connections={"gitlab": "gitlab.example.com"},
            app_server_starts=app_server_starts,
        )

        assert "AGENT_SERVER_URL" not in env
        assert env["AUTOMATION_RUN_MODE"] == "cloud"
        if app_server_starts:
            assert env["AUTOMATION_CONVERSATION_STARTED"] == "app_server"
            assert json.loads(env["AUTOMATION_RUNNER_REPOS"]) == [repos[1]]
        else:
            assert "AUTOMATION_CONVERSATION_STARTED" not in env
            assert "AUTOMATION_RUNNER_REPOS" not in env

    @pytest.mark.parametrize(
        "tarball",
        [
            pytest.param(lambda: _stored_preset("plugin", "git-synced"), id="preset"),
            pytest.param(lambda: build_tarball(SCRIPT_FILES), id="script"),
        ],
    )
    async def test_run_automation(self, tarball):
        env = await _blocking_run_env(tarball())

        assert "AGENT_SERVER_URL" not in env

    async def test_cloud_backend_env(self):
        backend = CloudSandboxBackend(api_url=API_URL, run=MagicMock())
        with patch(
            "openhands.automation.backends.cloud.get_api_key_for_automation_run",
            new_callable=AsyncMock,
            return_value="cloud-api-key",
        ):
            await backend.get_api_key()

        assert "AGENT_SERVER_URL" not in backend.build_env_vars()
