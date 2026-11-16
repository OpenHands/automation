"""Plugin-based automation script — runs inside an OpenHands execution environment.

This script is auto-generated from a plugin automation request. It supports two modes:

**Cloud Mode** (OpenHands Cloud and Enterprise):
  Uses OpenHandsCloudWorkspace connected to sandbox's agent server (localhost:3000).
  Full functionality: repos, skills, LLM, secrets, MCP from user's Cloud account.
  Requires: OPENHANDS_API_KEY, OPENHANDS_CLOUD_API_URL, SANDBOX_ID, SESSION_API_KEY

**Local Mode** (self-hosted):
  Uses RemoteWorkspace connected to a local agent server (AGENT_SERVER_URL).
  Full functionality: repos, skills, LLM, secrets, MCP from agent server settings.
  Requires: AUTOMATION_RUN_MODE=local (exported by the local backend), AGENT_SERVER_URL

Both workspace types share the same interface:
  - clone_repos() - clone repositories with auto-fetched tokens
  - load_skills_from_agent_server() - load skills via agent server API
  - get_repos_context() - generate context string for cloned repos
  - get_llm() - get LLM configuration
  - get_secrets() - get user secrets
  - get_mcp_config() - get MCP server configuration

The script:
  1. Detects mode from AUTOMATION_RUN_MODE (exported by the execution backend)
  2. Opens the workspace context EARLY (ensures callback on any failure)
  3. Clones repos via workspace.clone_repos()
  4. Loads skills via workspace.load_skills_from_agent_server()
  5. Gets LLM config via workspace.get_llm()
  6. Gets secrets via workspace.get_secrets()
  7. Gets MCP config via workspace.get_mcp_config(), or from users/me when an
     older SDK cannot read it, and logs servers that are configured but skipped
  8. Gets default agent with tools and condenser
  9. Loads plugins from plugins_config.json
  10. Creates a RemoteConversation with all plugins and sets a descriptive title.
      When the service started the conversation through the app server
      (AUTOMATION_CONVERSATION_STARTED=app_server), plugins included, steps 5-8
      are skipped and the script attaches to that conversation instead
  11. Sends the prompt (with event context if available) and runs
  12. On context manager exit, the workspace sends a completion callback

IMPORTANT: The workspace context is entered early so that ANY exception
(skill loading, prompt parsing, etc.) triggers the __exit__ callback,
avoiding silent failures that require watchdog timeout.

Env vars (Cloud mode - all required):
  OPENHANDS_API_KEY          - per-user automation API key
  OPENHANDS_CLOUD_API_URL    - SaaS API base URL
  SANDBOX_ID                 - this sandbox's Cloud API identifier
  OH_SESSION_API_KEYS_0      - session key for sandbox settings auth
                               (legacy fallback: SESSION_API_KEY)

Env vars (Local mode):
  AGENT_SERVER_URL           - local agent server URL (local mode only); it never
                               picks the mode
  OH_SESSION_API_KEYS_0      - API key for agent server auth (optional)
                               (legacy fallback: SESSION_API_KEY)

Common env vars:
  AUTOMATION_RUN_MODE        - "cloud" or "local", exported by the execution backend;
                               decides the mode. Without it (a service from 1.27.0 or
                               earlier), SANDBOX_ID means cloud and no SANDBOX_ID local.
  AUTOMATION_CALLBACK_URL    - completion callback endpoint (optional)
  AUTOMATION_ID              - automation ID for observability correlation (optional)
  AUTOMATION_NAME            - automation name for observability correlation (optional)
  AUTOMATION_RUN_ID          - run ID for the callback payload (optional)
  AUTOMATION_USER_ID         - observability attribution ID (optional): the owner's
                               user ID in cloud mode, this installation's ID in local mode
  AUTOMATION_ORG_ID          - owner org ID for observability context (optional)
  AUTOMATION_TRIGGER_SOURCE  - trigger source for observability context (optional)
  AUTOMATION_EVENT_PAYLOAD   - JSON with trigger info and event payload (optional)
  AUTOMATION_MODEL           - model profile name to load instead of default (optional)
  AUTOMATION_CONVERSATION_ID - the conversation to use (optional)
  AUTOMATION_CONVERSATION_STARTED - "app_server" when the service already started
                               AUTOMATION_CONVERSATION_ID through the app server,
                               plugins included; the script then attaches
  AUTOMATION_RUNNER_REPOS    - JSON list of the repositories this script still
                               clones when the app server cloned the others
                               (optional; otherwise repos_config.json)

Runtime-injected secrets (via conversation.update_secrets after Conversation creation):
  AUTOMATION_SESSION_URL     - direct URL to this conversation in Agent Canvas
                               (Cloud mode only; built from conversation.id)

"""

import inspect
import json
import os
import random
import sys
import threading
import time
import uuid
from datetime import datetime, timezone


def _detect_execution_mode(environ) -> tuple[str, str]:
    """Return ("cloud" | "local", the signal that decided it).

    The execution backend exports AUTOMATION_RUN_MODE, and it decides.
    AGENT_SERVER_URL never does, whether it is set or not: reading it as
    "local" made cloud runs fetch their LLM, secrets and MCP servers from the
    sandbox's own agent-server, which holds none of them (#688). Only a service
    from before AUTOMATION_RUN_MODE (1.27.0 and earlier) leaves it out; its cloud
    sandboxes carry SANDBOX_ID and its local backend does not.
    """
    explicit = environ.get("AUTOMATION_RUN_MODE", "").strip().lower()
    if explicit in ("cloud", "local"):
        return explicit, "AUTOMATION_RUN_MODE"
    if environ.get("SANDBOX_ID"):
        return "cloud", "SANDBOX_ID (no AUTOMATION_RUN_MODE)"
    return "local", "no SANDBOX_ID (no AUTOMATION_RUN_MODE)"


# Keep _detect_execution_mode in sync with presets/prompt and presets/plugin_spec.
EXECUTION_MODE, MODE_SIGNAL = _detect_execution_mode(os.environ)
IS_LOCAL_MODE = EXECUTION_MODE == "local"
agent_server_url = os.environ.get("AGENT_SERVER_URL", "").rstrip("/")


def _attaches_to_started_conversation(environ) -> bool:
    """Whether the service already started this run's conversation.

    On OpenHands Cloud/Enterprise the service asks the app server to start it
    in this sandbox, plugins included, so the run gets the owner's agent
    profile, MCP servers and secrets. The script then attaches instead of
    building its own agent. A derived id alone (``continue_conversation``)
    still means create.
    """
    return (
        bool(environ.get("AUTOMATION_CONVERSATION_ID"))
        and environ.get("AUTOMATION_CONVERSATION_STARTED") == "app_server"
    )


def _repo_instructions_section(skills) -> str | None:
    """Always-on instructions from the cloned repositories, as a prompt section.

    The conversation's system prompt was built without these repositories,
    so their AGENTS.md-style skills ride in the task message.
    Triggered and SKILL.md skills are left out; they would need registering.
    """
    parts = [
        f"### {skill.name}\n\n{skill.content}"
        for skill in skills
        if skill.trigger is None and not getattr(skill, "is_agentskills_format", False)
    ]
    if not parts:
        return None
    return "## Repository instructions\n\n" + "\n\n".join(parts)


def _listed_runner_repos(environ, attaches: bool):
    """The repositories left to this script when the app server cloned the others.

    None means clone every repository in repos_config.json, as before.
    """
    listed = environ.get("AUTOMATION_RUNNER_REPOS")
    return json.loads(listed) if attaches and listed is not None else None


ATTACHES_TO_STARTED_CONVERSATION = _attaches_to_started_conversation(os.environ)


def _local_launch_profile(environ, attaches: bool, is_local: bool):
    """The agent profile this script starts its conversation from, or None.

    Only a local agent server holds the owner's profiles; a cloud sandbox's
    does not, so a cloud run the service did not start keeps the default agent.
    """
    if attaches or not is_local:
        return None
    return environ.get("AUTOMATION_AGENT_PROFILE_ID") or None


LAUNCH_PROFILE_ID = _local_launch_profile(
    os.environ, ATTACHES_TO_STARTED_CONVERSATION, IS_LOCAL_MODE
)
SERVER_BUILDS_AGENT = ATTACHES_TO_STARTED_CONVERSATION or LAUNCH_PROFILE_ID is not None


def _attach_started_conversation(workspace, conversation_id: str, callbacks=None):
    """Attach to the conversation the service started, as this run's conversation.

    RemoteConversation.attach() does not register the conversation with the
    workspace the way create() does. The completion callback reports only a
    registered one, and without it the run loses its link to the conversation
    and its finish result.
    """
    conversation = RemoteConversation.attach(
        workspace, uuid.UUID(conversation_id), callbacks=callbacks
    )
    register = getattr(workspace, "register_conversation", None)
    if callable(register):
        register(str(conversation.id))
    return conversation


def _start_profile_conversation(
    workspace, agent_profile_id: str, llm_profile_ref=None, callbacks=None, **kwargs
):
    """Create this run's conversation on the agent server from an agent profile."""
    if llm_profile_ref:
        kwargs["agent_launch_additions"] = AgentLaunchAdditions(
            llm_profile_ref=llm_profile_ref
        )
    request = StartConversationRequest(
        agent_profile_id=uuid.UUID(agent_profile_id),
        workspace=LocalWorkspace(working_dir=workspace.working_dir),
        **kwargs,
    )
    return RemoteConversation.create(workspace, request, callbacks=callbacks)


# Cloud mode env vars
api_key = os.environ.get("OPENHANDS_API_KEY", "")
api_url = os.environ.get("OPENHANDS_CLOUD_API_URL", "").rstrip("/")
sandbox_id = os.environ.get("SANDBOX_ID", "")
# Prefer OH_SESSION_API_KEYS_0 — the canonical agent-server env var that is
# inherited unmodified by bash subprocesses. The legacy SESSION_API_KEY name
# is stripped by the SDK's sanitized_env() defense-in-depth filter, so it
# may be missing here even when the agent-server has a valid session key.
# We still fall back to SESSION_API_KEY for compatibility with cloud-mode
# deployments and older agent-server versions that only set the bare name.
session_key = os.environ.get("OH_SESSION_API_KEYS_0") or os.environ.get(
    "SESSION_API_KEY", ""
)
model_profile = os.environ.get("AUTOMATION_MODEL") or None
automation_user_id = os.environ.get("AUTOMATION_USER_ID") or None

print("=== EXECUTION MODE ===")
print(f"  mode: {'LOCAL' if IS_LOCAL_MODE else 'CLOUD'} (decided by {MODE_SIGNAL})")

print("\n=== ENV VARS ===")
if IS_LOCAL_MODE:
    # Local mode: AGENT_SERVER_URL required
    print(f"  AGENT_SERVER_URL: {'OK' if agent_server_url else 'MISSING'}")
    print(f"  OH_SESSION_API_KEYS_0: {'OK' if session_key else 'NONE (may fail auth)'}")
    if not agent_server_url:
        print("FAIL: AGENT_SERVER_URL not set for local mode", file=sys.stderr)
        sys.exit(1)
else:
    # Cloud mode: all Cloud env vars are required
    for name, val in [
        ("OPENHANDS_API_KEY", api_key),
        ("OPENHANDS_CLOUD_API_URL", api_url),
        ("SANDBOX_ID", sandbox_id),
        ("OH_SESSION_API_KEYS_0", session_key),
    ]:
        print(f"  {name}: {'OK' if val else 'MISSING'}")
        if not val:
            print(f"FAIL: {name} not set", file=sys.stderr)
            sys.exit(1)

print(
    f"  AUTOMATION_CALLBACK_URL: {os.environ.get('AUTOMATION_CALLBACK_URL') or 'NONE'}"
)
print(f"  AUTOMATION_MODEL: {model_profile or 'DEFAULT'}")
print(f"  AUTOMATION_USER_ID: {'OK' if automation_user_id else 'NONE'}")
print(f"  AUTOMATION_ORG_ID: {'OK' if os.environ.get('AUTOMATION_ORG_ID') else 'NONE'}")
print(f"  AUTOMATION_RUN_ID: {os.environ.get('AUTOMATION_RUN_ID') or 'NONE'}")

# --- Live phase reporting (best-effort, never fatal) -------------------------
# Keep this block in sync with presets/prompt/sdk_main.py.
AUTOMATION_PHASE_URL = os.environ.get("AUTOMATION_PHASE_URL", "")
_PHASE_TOKEN = (
    os.environ.get("AUTOMATION_CALLBACK_API_KEY")
    or os.environ.get("OPENHANDS_API_KEY")
    or ""
)
PHASE_POST_INTERVAL_SECONDS = 4.0


def report_phase(message: str) -> None:
    """POST a short progress phase to the automation service. Never raises."""
    if not AUTOMATION_PHASE_URL or not _PHASE_TOKEN or not message:
        return
    try:
        import httpx  # installed alongside the SDK by setup.sh

        httpx.post(
            AUTOMATION_PHASE_URL,
            json={"phase": message[:200]},
            headers={"Authorization": f"Bearer {_PHASE_TOKEN}"},
            timeout=5.0,
        )
    except Exception:
        pass  # phases are cosmetic; the run must never fail because of them


def _clone_configured_repos(workspace, config_file: str, listed=None):
    """Clone the automation's repositories, or fail the run if any cannot be.

    A run without its repository would work on nothing and still report
    success, so a failed clone stops the run with the reason instead.
    ``listed`` replaces repos_config.json when the app server cloned the rest.
    """
    if listed is not None:
        repos_config = listed
    elif os.path.exists(config_file):
        with open(config_file) as f:
            repos_config = json.load(f)
    else:
        return None
    if not repos_config:
        return None
    print("\n=== CLONE REPOS ===")
    report_phase("Cloning repositories")
    clone_result = workspace.clone_repos(repos_config)
    print(f"  cloned {clone_result.success_count}/{len(repos_config)} repos")
    if clone_result.failed_repos:
        raise RuntimeError(
            "Could not clone the automation's repositories: "
            + ", ".join(clone_result.failed_repos)
        )
    return clone_result


def _redact_phase(text: str) -> str:
    """Redact secrets from a phase; drop it entirely if redaction is missing."""
    try:
        from openhands.sdk.utils.redact import (
            redact_api_key_literals,
            redact_text_secrets,
        )

        return redact_api_key_literals(redact_text_secrets(text))
    except Exception:
        return ""


# Latest pending live phase; a daemon thread posts it at most once per
# interval so the conversation event thread never blocks on network I/O.
# Deliberately unlocked: each key is a single reference read/write (atomic
# under the GIL) and last-writer-wins is acceptable for cosmetic telemetry.
_live_phase: dict = {"pending": None, "posted": None, "stop": False}


def _phase_poster() -> None:
    while not _live_phase["stop"]:
        time.sleep(PHASE_POST_INTERVAL_SECONDS)
        message = _live_phase["pending"]
        if message and message != _live_phase["posted"]:
            _live_phase["posted"] = message
            report_phase(message)


# SDK imports (before workspace context so import errors are caught)
from openhands.sdk import Conversation, RemoteConversation
from finish_tool_hook import finish_tool_required_hook_config
from openhands.sdk.automation import automation_conversation_kwargs
from openhands.tools.preset import TaskOutcome

try:
    from openhands.sdk.mcp.config import coerce_mcp_config as _coerce_mcp_config
except ImportError:
    _coerce_mcp_config = None
from openhands.sdk.plugin import PluginSource
from openhands.sdk.conversation.request import (
    AgentLaunchAdditions,
    StartConversationRequest,
)
from openhands.sdk.workspace.local import LocalWorkspace
from openhands.sdk.workspace.remote.base import RemoteWorkspace
from openhands.tools.preset.default import get_default_agent
from openhands.workspace import OpenHandsCloudWorkspace


def _conversation_supports_user_id() -> bool:
    try:
        return "user_id" in inspect.signature(Conversation.__new__).parameters
    except (TypeError, ValueError):
        return False


def _normalize_mcp_config(raw_mcp_config):
    if not raw_mcp_config:
        return {}
    if _coerce_mcp_config is not None:
        return _coerce_mcp_config(raw_mcp_config)
    if isinstance(raw_mcp_config, dict) and isinstance(
        raw_mcp_config.get("mcpServers"), dict
    ):
        return raw_mcp_config["mcpServers"]
    return raw_mcp_config


# Keep the MCP helpers below in sync with presets/prompt/sdk_main.py.
def _users_me_mcp_config():
    """The owner's MCP servers as GET /api/v1/users/me returns them (cloud mode)."""
    import httpx  # installed alongside the SDK by setup.sh

    response = httpx.get(
        f"{api_url}/api/v1/users/me",
        params={"expose_secrets": "true"},
        headers={"Authorization": f"Bearer {api_key}", "X-Session-API-Key": session_key},
        timeout=60.0,
    )
    response.raise_for_status()
    return response.json().get("mcp_config")


def _named_mcp_servers(raw_mcp_config):
    """Split a {name: server} map into (servers to load, {name: reason skipped})."""
    if isinstance(raw_mcp_config, dict) and "mcpServers" in raw_mcp_config:
        raw_mcp_config = raw_mcp_config["mcpServers"]
    servers, skipped = {}, {}
    if not isinstance(raw_mcp_config, dict):
        return servers, skipped
    for name, raw_server in raw_mcp_config.items():
        auth = raw_server.get("auth") if isinstance(raw_server, dict) else None
        # The app server keeps OAuth tokens and saves the ones a conversation
        # refreshes. A conversation this script creates cannot save a refreshed
        # token back; one the app server starts for the run can.
        if isinstance(auth, dict) and auth.get("strategy") == "oauth2":
            skipped[name] = (
                "it signs in with OAuth, which needs the app server to start "
                "the run's conversation"
            )
            continue
        try:
            servers.update(_normalize_mcp_config({name: raw_server}))
        except Exception:
            # The error would echo the server's settings, secrets included.
            skipped[name] = "its settings could not be read"
    return servers, skipped


def _load_mcp_config(workspace):
    """Return the run's MCP servers, printing what was loaded and what was not.

    openhands-sdk 1.59 and earlier read only the retired sse/shttp/stdio server
    lists, so get_mcp_config() returns {} for the {name: server} map users/me
    serves today (OpenHands/automation#93); read that map here instead.
    """
    mcp_config = _normalize_mcp_config(workspace.get_mcp_config())
    skipped = {}
    if not mcp_config and not IS_LOCAL_MODE:
        mcp_config, skipped = _named_mcp_servers(_users_me_mcp_config())
    if mcp_config:
        print(f"  servers: {list(mcp_config.keys())}")
    for name, reason in skipped.items():
        print(f"  WARNING: MCP server {name!r} is configured but not loaded: {reason}")
    if not mcp_config and not skipped:
        print("  no MCP servers configured")
    return mcp_config


def _build_conversation_title(event_context) -> str | None:
    """Build a descriptive conversation title from the automation event context.

    Returns "{automation_name} — {repo}#{number}" for PR/issue events,
    including Azure DevOps and Bitbucket Data Center pull requests,
    "{automation_name} — {issue key}" for Jira Data Center issue events,
    "{automation_name} — {timestamp}" otherwise, or None when no automation
    name is available (keeps the agent-server's default autotitle behavior).
    """
    if not isinstance(event_context, dict):
        return None
    name = event_context.get("automation_name")
    if not isinstance(name, str) or not name.strip():
        return None
    name = " ".join(name.split())

    context = None
    event = event_context.get("event")
    if isinstance(event, dict):
        repo = event.get("repository")
        repo_name = repo.get("full_name") if isinstance(repo, dict) else None
        number = None
        for key in ("pull_request", "issue"):
            obj = event.get(key)
            if isinstance(obj, dict):
                candidate = obj.get("number")
                if isinstance(candidate, int) and not isinstance(candidate, bool):
                    number = candidate
                    break
        if number is None:
            candidate = event.get("number")
            if isinstance(candidate, int) and not isinstance(candidate, bool):
                number = candidate
        if isinstance(repo_name, str) and repo_name and number is not None:
            context = f"{repo_name.rsplit('/', 1)[-1]}#{number}"
        # Azure DevOps Service Hook events arrive wrapped. The pull request is
        # the resource itself, or sits under it for a pull request comment.
        raw = event.get("payload")
        resource = raw.get("resource") if isinstance(raw, dict) else None
        if context is None and isinstance(resource, dict):
            pull_request = resource.get("pullRequest")
            if not isinstance(pull_request, dict):
                pull_request = resource
            ado_repo = pull_request.get("repository")
            ado_repo_name = ado_repo.get("name") if isinstance(ado_repo, dict) else None
            ado_number = pull_request.get("pullRequestId")
            if (
                isinstance(ado_repo_name, str)
                and ado_repo_name
                and isinstance(ado_number, int)
                and not isinstance(ado_number, bool)
            ):
                context = f"{ado_repo_name}#{ado_number}"
        # Jira Data Center and Bitbucket Data Center events arrive wrapped the
        # same way. Bitbucket names the pull request's target repository by its
        # slug; a Jira issue is best known by its key.
        if context is None and isinstance(raw, dict):
            pull_request = raw.get("pullRequest")
            issue = raw.get("issue")
            if isinstance(pull_request, dict):
                to_ref = pull_request.get("toRef")
                dc_repo = to_ref.get("repository") if isinstance(to_ref, dict) else None
                dc_slug = dc_repo.get("slug") if isinstance(dc_repo, dict) else None
                dc_number = pull_request.get("id")
                if (
                    isinstance(dc_slug, str)
                    and dc_slug
                    and isinstance(dc_number, int)
                    and not isinstance(dc_number, bool)
                ):
                    context = f"{dc_slug}#{dc_number}"
            elif isinstance(issue, dict):
                issue_key = issue.get("key")
                if isinstance(issue_key, str) and issue_key.strip():
                    context = issue_key.strip()
    if context is None:
        context = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return f"{name} — {context}"[:200]


# Workspace base directory (for RemoteWorkspace working_dir)
# Expand ~ to home directory before validation
workspace_base = os.path.expanduser(os.environ.get("WORKSPACE_BASE", "/workspace"))

# Validate workspace_base path (after expansion) - fail fast with clear errors
if not os.path.isabs(workspace_base):
    print(
        f"ERROR: WORKSPACE_BASE must be absolute path, got: {workspace_base}",
        file=sys.stderr,
    )
    sys.exit(1)
if IS_LOCAL_MODE and not os.path.isdir(workspace_base):
    print(
        f"ERROR: WORKSPACE_BASE directory does not exist: {workspace_base}",
        file=sys.stderr,
    )
    sys.exit(1)

# Create workspace based on mode
# Both workspace types share the same interface for repos/skills/LLM/secrets/MCP
print("\n=== SDK WORKSPACE ===")
if IS_LOCAL_MODE:
    # Local mode: use RemoteWorkspace connected to local agent server
    print(f"  using RemoteWorkspace at {agent_server_url}")
    print(f"  working_dir: {workspace_base}")
    workspace_ctx = RemoteWorkspace(
        host=agent_server_url,
        api_key=session_key if session_key else None,
        working_dir=workspace_base,
    )
else:
    # Cloud mode: use OpenHandsCloudWorkspace connected to sandbox's agent server
    print(f"  using OpenHandsCloudWorkspace at {api_url}")
    workspace_ctx = OpenHandsCloudWorkspace(
        local_agent_server_mode=True,
        cloud_api_url=api_url,
        cloud_api_key=api_key,
        keep_alive=True,
    )

# Enter workspace context EARLY - any exception from here on triggers callback
with workspace_ctx as workspace:
    # -- All remaining setup happens inside the workspace context --
    # This ensures failures trigger the __exit__ callback
    report_phase("Setting up workspace")

    # Parse event payload if present (for event-triggered automations)
    event_context = None
    if event_payload_json := os.environ.get("AUTOMATION_EVENT_PAYLOAD"):
        try:
            event_context = json.loads(event_payload_json)
        except json.JSONDecodeError as e:
            print(
                f"ERROR: Failed to parse AUTOMATION_EVENT_PAYLOAD: {e}", file=sys.stderr
            )

    # Clone repositories if repos_config.json exists
    SCRIPT_DIR = os.path.dirname(__file__)
    REPOS_CONFIG_FILE = os.path.join(SCRIPT_DIR, "repos_config.json")
    clone_result = _clone_configured_repos(
        workspace,
        REPOS_CONFIG_FILE,
        _listed_runner_repos(os.environ, ATTACHES_TO_STARTED_CONVERSATION),
    )
    # Collect cloned repo directories for skill loading
    repo_dirs = (
        [m.local_path for m in clone_result.repo_mappings.values()]
        if clone_result
        else []
    )

    # Load ALL skills via workspace.load_skills_from_agent_server()
    # If repos were cloned, project skills are loaded from EACH cloned repo
    print("\n=== LOAD SKILLS ===")
    report_phase("Loading skills")
    if SERVER_BUILDS_AGENT:
        # The conversation's server loads the user's skills with the agent;
        # only the repositories cloned here are new to it.
        agent_context = None
        loaded_skills = []
        if repo_dirs:
            loaded_skills, _ = workspace.load_skills_from_agent_server(
                project_dirs=repo_dirs,
                load_public=False,
                load_user=False,
                load_org=False,
            )
    else:
        loaded_skills, agent_context = workspace.load_skills_from_agent_server(
            project_dirs=repo_dirs if repo_dirs else None
        )
    print(f"  loaded {len(loaded_skills)} skills")

    # Get repos context (mapping of URLs to local paths)
    repos_context = ""
    if clone_result and clone_result.repo_mappings:
        repos_context = workspace.get_repos_context(clone_result.repo_mappings)

    # Load configuration files
    EXPERIMENT_CONFIG_FILE = os.path.join(SCRIPT_DIR, "experiment_config.json")
    PLUGINS_CONFIG_FILE = os.path.join(SCRIPT_DIR, "plugins_config.json")
    PROMPT_FILE = os.path.join(SCRIPT_DIR, "prompt.txt")

    # Experiment-aware variant selection
    experiment_id: str | None = None
    selected_variant: str | None = None

    if os.path.exists(EXPERIMENT_CONFIG_FILE):
        with open(EXPERIMENT_CONFIG_FILE) as f:
            experiment_config = json.load(f)

        experiment_id = experiment_config["experiment_id"]
        variants = experiment_config["variants"]
        weights = [v["weight"] for v in variants]
        selected = random.choices(variants, weights=weights, k=1)[0]

        selected_variant = selected["name"]
        model_profile = selected.get("model") or model_profile
        plugins_config = selected["plugins"]
        print("\n=== EXPERIMENT ===")
        print(f"  id: {experiment_id}")
        print(f"  variant: {selected_variant}")
        print(f"  model_profile: {model_profile or 'DEFAULT'}")
        print(f"  weights: {dict(zip([v['name'] for v in variants], weights))}")
    else:
        with open(PLUGINS_CONFIG_FILE) as f:
            plugins_config = json.load(f)

    with open(PROMPT_FILE) as f:
        USER_PROMPT = f.read()

    # Build prompt with context sections
    context_sections = []

    # Add repos context if repos were cloned
    if repos_context:
        context_sections.append(repos_context)
    if SERVER_BUILDS_AGENT:
        repo_instructions = _repo_instructions_section(loaded_skills)
        if repo_instructions:
            context_sections.append(repo_instructions)

    # Add event context if this is an event-triggered run
    if event_context and "event" in event_context:
        event_json = json.dumps(event_context["event"], indent=2)
        context_sections.append(f"""## Event Payload

This automation was triggered by a webhook event. The full event payload is
included below; use it directly. Do not read it from a shell environment
variable such as `$AUTOMATION_EVENT_PAYLOAD`: the automation process receives
that variable, but your terminal does not inherit it.

```json
{event_json}
```""")

    # More events landed on the same subject before this run got a sandbox, so
    # the service could not deliver them as turns. They open the conversation
    # with this one instead of each starting a run of its own.
    if event_context and event_context.get("follow_up_turns"):
        follow_ups = "\n\n".join(str(turn) for turn in event_context["follow_up_turns"])
        context_sections.append(f"""## Follow-up messages

More activity arrived on the same subject while this run was queued:

{follow_ups}""")

    # Prepend context sections to the user prompt
    if context_sections:
        context_block = "\n\n".join(context_sections)
        USER_PROMPT = f"""{context_block}

## Task

{USER_PROMPT}"""

    # Deserialize plugin sources using Pydantic validation
    plugin_sources = [PluginSource.model_validate(p) for p in plugins_config]

    print("\n=== PLUGINS CONFIG ===")
    print(f"  loading {len(plugin_sources)} plugin(s):")
    for ps in plugin_sources:
        ref_str = f"@{ps.ref}" if ps.ref else ""
        path_str = f" ({ps.repo_path})" if ps.repo_path else ""
        print(f"    - {ps.source}{ref_str}{path_str}")

    received_events: list = []
    last_event_time = {"ts": time.time()}

    def event_callback(event) -> None:
        received_events.append(event)
        last_event_time["ts"] = time.time()
        # Surface the LLM-authored ~10-word action summary as a live phase.
        # Name-based check keeps this resilient across SDK versions.
        if type(event).__name__ == "ActionEvent":
            summary = getattr(event, "summary", None)
            if isinstance(summary, str) and summary.strip():
                redacted = _redact_phase(" ".join(summary.split()))
                if redacted:
                    _live_phase["pending"] = redacted[:200]

    # Build experiment tags (if running an A/B test)
    experiment_tags: dict[str, str] = {}
    if experiment_id:
        experiment_tags["experimentid"] = experiment_id
        if selected_variant is None:
            raise RuntimeError(
                "BUG: experiment_id is set but selected_variant is None — "
                "experiment config may be malformed."
            )
        experiment_tags["variant"] = selected_variant
        if model_profile:
            experiment_tags["modelprofile"] = model_profile

    automation_conversation_id = os.environ.get("AUTOMATION_CONVERSATION_ID")
    secrets = {}
    if ATTACHES_TO_STARTED_CONVERSATION:
        # The app server started this conversation from the owner's agent
        # profile, with their LLM key, MCP servers, secrets and these plugins.
        # Attach to it; recreating it here would lose all of that.
        print("\n=== CONVERSATION ===")
        report_phase("Configuring agent")
        conversation = _attach_started_conversation(
            workspace, automation_conversation_id, callbacks=[event_callback]
        )
        print(f"  attached to conversation {conversation.id} (started by app server)")
    elif LAUNCH_PROFILE_ID:
        # The agent server builds the agent, LLM and MCP servers from the
        # profile, as it does for a chat started from it.
        print("\n=== GET_SECRETS ===")
        try:
            secrets = workspace.get_secrets(agent_profile_id=LAUNCH_PROFILE_ID)
            print(f"  available: {list(secrets.keys()) or '(none)'}")
        except Exception as e:
            print(f"  get_secrets() failed (ok if no secrets): {e}")

        print("\n=== CONVERSATION ===")
        report_phase("Configuring agent")
        request_kwargs = {
            "plugins": plugin_sources,
            "hook_config": finish_tool_required_hook_config(SCRIPT_DIR),
            **automation_conversation_kwargs(conversation_tags=experiment_tags),
        }
        if automation_user_id and _conversation_supports_user_id():
            request_kwargs["user_id"] = automation_user_id
        if automation_conversation_id:
            request_kwargs["conversation_id"] = uuid.UUID(automation_conversation_id)
        conversation = _start_profile_conversation(
            workspace,
            LAUNCH_PROFILE_ID,
            llm_profile_ref=model_profile,
            callbacks=[event_callback],
            **request_kwargs,
        )
        print(f"  conversation created from agent profile {LAUNCH_PROFILE_ID}")
    else:
        # Get LLM config via workspace/profile APIs
        print("\n=== GET_LLM ===")
        try:
            llm = workspace.get_llm(profile_name=model_profile)
        except FileNotFoundError:
            if not model_profile:
                raise
            print(
                f"  profile {model_profile!r} not found; "
                "falling back to active/default profile"
            )
            llm = workspace.get_llm()
        print(f"  profile: {model_profile or 'DEFAULT'}")
        print(f"  model: {llm.model}")
        print(f"  api_key present: {bool(llm.api_key)}")

        # Get secrets via workspace
        print("\n=== GET_SECRETS ===")
        try:
            secrets = workspace.get_secrets()
            print(f"  available: {list(secrets.keys()) or '(none)'}")
        except Exception as e:
            # Not a hard failure — user may not have secrets configured
            print(f"  get_secrets() failed (ok if no secrets): {e}")

        # Get MCP config via workspace
        print("\n=== GET_MCP_CONFIG ===")
        mcp_config = {}
        try:
            mcp_config = _load_mcp_config(workspace)
        except Exception as e:
            # Not a hard failure: the run continues without MCP servers
            print(f"  WARNING: MCP servers not loaded: {type(e).__name__}: {e}")

        # Get default agent with tools and condenser (CLI mode to disable browser)
        print("\n=== AGENT ===")
        report_phase("Configuring agent")
        # Keep finish-tool schema wiring in sync with presets/prompt/sdk_main.py.
        agent = get_default_agent(
            llm=llm,
            cli_mode=True,
            finish_tool_response_schema=TaskOutcome,
        )

        # Add MCP config and agent_context using model_copy if configured
        # (Plugin MCP configs will be merged when plugins are loaded)
        agent_updates = {}
        if mcp_config:
            agent_updates["mcp_config"] = mcp_config
        if agent_context:
            agent_updates["agent_context"] = agent_context
        if agent_updates:
            agent = agent.model_copy(update=agent_updates)

        print(f"  tools: {[t.name for t in agent.tools]}")
        print(f"  mcp_config: {'configured' if mcp_config else 'none'}")
        print(f"  skills: {len(loaded_skills) if loaded_skills else 0}")
        condenser_name = type(agent.condenser).__name__ if agent.condenser else "none"
        print(f"  condenser: {condenser_name}")

        # Create conversation with plugins
        print("\n=== CONVERSATION ===")
        conversation_kwargs = {
            "agent": agent,
            "workspace": workspace,
            "plugins": plugin_sources,  # All plugins loaded here
            "callbacks": [event_callback],
            "hook_config": finish_tool_required_hook_config(SCRIPT_DIR),
            "delete_on_close": False,  # Keep conversation history after completion
            **automation_conversation_kwargs(conversation_tags=experiment_tags),
        }
        if automation_user_id and _conversation_supports_user_id():
            conversation_kwargs["user_id"] = automation_user_id
        # A `continue_conversation` run must use the derived id: RemoteConversation
        # attaches to that conversation if it exists, and creates it if it does not.
        if automation_conversation_id:
            conversation_kwargs["conversation_id"] = uuid.UUID(
                automation_conversation_id
            )
        conversation = Conversation(**conversation_kwargs)
        assert isinstance(conversation, RemoteConversation)
        print(f"  conversation created: {type(conversation).__name__}")
        print(f"  plugins loaded: {len(plugin_sources)}")
        if experiment_tags:
            print(f"  experiment tags: {experiment_tags}")

    # Set a descriptive title so automation runs are distinguishable in the
    # conversation panel — otherwise the agent-server's autotitle falls back
    # to a truncated prompt prefix. Failure is non-fatal; the run continues.
    conversation_title = _build_conversation_title(event_context)
    if conversation_title:
        try:
            resp = workspace.client.patch(
                f"/api/conversations/{conversation.id}",
                json={"title": conversation_title},
            )
            resp.raise_for_status()
            print(f"  title: {conversation_title}")
        except Exception as e:
            # Not a hard failure — autotitle fallback still applies
            print(f"  set title failed (non-fatal): {e}")

    # Inject secrets into the conversation (auto-exported as env vars in bash)
    if secrets:
        conversation.update_secrets(secrets)
        print(f"  injected {len(secrets)} secrets into conversation")

    # Build session URL from conversation ID and inject as a secret so
    # the agent can use $AUTOMATION_SESSION_URL in bash commands.
    if not IS_LOCAL_MODE and api_url:
        session_url = f"{api_url}/canvas/conversations/{conversation.id}"
        conversation.update_secrets({"AUTOMATION_SESSION_URL": session_url})
        print(f"  session URL: {session_url}")

    report_phase("Agent is working on the task")
    threading.Thread(target=_phase_poster, name="phase-poster", daemon=True).start()

    cost = None
    try:
        print(f"  sending prompt: {USER_PROMPT[:80]}...")
        conversation.send_message(USER_PROMPT)
        conversation.run()

        # Post-run bookkeeping is best-effort: the conversation has already
        # succeeded, and nothing below may raise out of the workspace context
        # or the __exit__ callback would report FAILED for a successful run.
        # The callback's cost comes from conversation.close() (cached stats),
        # not from this live fetch.
        try:
            # Wait for the stream to settle
            while time.time() - last_event_time["ts"] < 2.0:
                time.sleep(0.1)

            cost = (
                conversation.conversation_stats.get_combined_metrics().accumulated_cost
            )
        except Exception as e:
            print(f"  WARNING: post-run stats fetch failed (non-fatal): {e}")
        print(f"  cost: {cost}")
        print(f"  events received: {len(received_events)}")
    finally:
        _live_phase["stop"] = True
        try:
            conversation.close()
        except Exception as e:
            print(f"  WARNING: conversation.close() failed (non-fatal): {e}")

    print("  conversation completed successfully")

    print("\n=== RESULT ===")
    print("ALL_OK")
