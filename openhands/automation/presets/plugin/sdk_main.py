"""Plugin-based automation script — runs inside an OpenHands execution environment.

This script is auto-generated from a plugin automation request. It supports two modes:

**Cloud Mode** (default):
  Uses OpenHandsCloudWorkspace connected to sandbox's agent server (localhost:3000).
  Full functionality: repos, skills, LLM, secrets, MCP from user's Cloud account.
  Requires: OPENHANDS_API_KEY, OPENHANDS_CLOUD_API_URL, SANDBOX_ID, SESSION_API_KEY

**Local Mode** (self-hosted):
  Uses RemoteWorkspace connected to a local agent server (AGENT_SERVER_URL).
  Full functionality: repos, skills, LLM, secrets, MCP from agent server settings.
  Requires: AGENT_SERVER_URL (presence triggers local mode)

Both workspace types share the same interface:
  - clone_repos() - clone repositories with auto-fetched tokens
  - load_skills_from_agent_server() - load skills via agent server API
  - get_repos_context() - generate context string for cloned repos
  - get_llm() - get LLM configuration
  - get_secrets() - get user secrets
  - get_mcp_config() - get MCP server configuration

The script:
  1. Detects mode based on AGENT_SERVER_URL presence
  2. Opens the workspace context EARLY (ensures callback on any failure)
  3. Clones repos via workspace.clone_repos()
  4. Loads skills via workspace.load_skills_from_agent_server()
  5. Gets LLM config via workspace.get_llm()
  6. Gets secrets via workspace.get_secrets()
  7. Gets MCP config via workspace.get_mcp_config()
  8. Gets default agent with tools and condenser
  9. Loads plugins from plugins_config.json
  10. Creates a RemoteConversation with all plugins
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
  AGENT_SERVER_URL           - local agent server URL (presence = local mode)
  OH_SESSION_API_KEYS_0      - API key for agent server auth (optional)
                               (legacy fallback: SESSION_API_KEY)

Common env vars:
  AUTOMATION_CALLBACK_URL    - completion callback endpoint (optional)
  AUTOMATION_RUN_ID          - run ID for the callback payload (optional)
  AUTOMATION_USER_ID         - owner user ID for observability attribution (optional)
  AUTOMATION_ORG_ID          - owner org ID for observability context (optional)
  AUTOMATION_EVENT_PAYLOAD   - JSON with trigger info and event payload (optional)
  AUTOMATION_MODEL           - model profile name to load instead of default (optional)

Runtime-injected secrets (via conversation.update_secrets after Conversation creation):
  AUTOMATION_SESSION_URL     - direct URL to this conversation in the OpenHands UI
                               (Cloud mode only; built from conversation.id)

"""

import inspect
import json
import os
import random
import sys
import time

# Detect execution mode based on AGENT_SERVER_URL presence
agent_server_url = os.environ.get("AGENT_SERVER_URL", "").rstrip("/")
IS_LOCAL_MODE = bool(agent_server_url)

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
session_key = (
    os.environ.get("OH_SESSION_API_KEYS_0")
    or os.environ.get("SESSION_API_KEY", "")
)
model_profile = os.environ.get("AUTOMATION_MODEL") or None
automation_user_id = os.environ.get("AUTOMATION_USER_ID") or None

print("=== EXECUTION MODE ===")
print(f"  mode: {'LOCAL' if IS_LOCAL_MODE else 'CLOUD'}")

print("\n=== ENV VARS ===")
if IS_LOCAL_MODE:
    # Local mode: AGENT_SERVER_URL required
    print(f"  AGENT_SERVER_URL: {'OK' if agent_server_url else 'MISSING'}")
    print(
        f"  OH_SESSION_API_KEYS_0: "
        f"{'OK' if session_key else 'NONE (may fail auth)'}"
    )
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

# SDK imports (before workspace context so import errors are caught)
from openhands.sdk import Conversation, RemoteConversation
from openhands.sdk.plugin import PluginSource
from openhands.sdk.workspace.remote.base import RemoteWorkspace
from openhands.tools.preset.default import get_default_agent
from openhands.workspace import OpenHandsCloudWorkspace


def _conversation_supports_user_id() -> bool:
    try:
        return "user_id" in inspect.signature(Conversation.__new__).parameters
    except (TypeError, ValueError):
        return False


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
    )

# Enter workspace context EARLY - any exception from here on triggers callback
with workspace_ctx as workspace:
    # -- All remaining setup happens inside the workspace context --
    # This ensures failures trigger the __exit__ callback

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
    clone_result = None
    repo_dirs = []

    if os.path.exists(REPOS_CONFIG_FILE):
        print("\n=== CLONE REPOS ===")
        with open(REPOS_CONFIG_FILE) as f:
            repos_config = json.load(f)
        if repos_config:
            clone_result = workspace.clone_repos(repos_config)
            print(f"  cloned {clone_result.success_count}/{len(repos_config)} repos")
            if clone_result.failed_repos:
                print(f"  FAILED: {', '.join(clone_result.failed_repos)}")
            # Collect cloned repo directories for skill loading
            repo_dirs = [m.local_path for m in clone_result.repo_mappings.values()]

    # Load ALL skills via workspace.load_skills_from_agent_server()
    # If repos were cloned, project skills are loaded from EACH cloned repo
    print("\n=== LOAD SKILLS ===")
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
        plugins_config = selected["plugins"]
        print("\n=== EXPERIMENT ===")
        print(f"  id: {experiment_id}")
        print(f"  variant: {selected_variant}")
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

    # Add event context if this is an event-triggered run
    if event_context and "event" in event_context:
        event_json = json.dumps(event_context["event"], indent=2)
        context_sections.append(f"""## Event Payload

This automation was triggered by a webhook event:

```json
{event_json}
```""")

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
    secrets = {}
    try:
        secrets = workspace.get_secrets()
        print(f"  available: {list(secrets.keys()) or '(none)'}")
    except Exception as e:
        # Not a hard failure — user may not have secrets configured
        print(f"  get_secrets() failed (ok if no secrets): {e}")

    # Get MCP config via workspace
    print("\n=== GET_MCP_CONFIG ===")
    mcp_config = None
    try:
        mcp_config = workspace.get_mcp_config()
        if mcp_config and mcp_config.get("mcpServers"):
            print(f"  servers: {list(mcp_config['mcpServers'].keys())}")
        else:
            print("  no MCP servers configured")
    except Exception as e:
        # Not a hard failure — user may not have MCP configured
        print(f"  get_mcp_config() failed (ok if no MCP): {e}")

    # Get default agent with tools and condenser (CLI mode to disable browser)
    print("\n=== AGENT ===")
    agent = get_default_agent(llm=llm, cli_mode=True)

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

    received_events: list = []
    last_event_time = {"ts": time.time()}

    def event_callback(event) -> None:
        received_events.append(event)
        last_event_time["ts"] = time.time()

    # Build experiment tags (if running an A/B test)
    experiment_tags: dict[str, str] = {}
    if experiment_id:
        experiment_tags["experiment_id"] = experiment_id
        if selected_variant is None:
            raise RuntimeError(
                "BUG: experiment_id is set but selected_variant is None — "
                "experiment config may be malformed."
            )
        experiment_tags["variant"] = selected_variant

    conversation_kwargs = {
        "agent": agent,
        "workspace": workspace,
        "plugins": plugin_sources,  # All plugins loaded here
        "callbacks": [event_callback],
        "delete_on_close": False,  # Keep conversation history after completion
        "tags": experiment_tags or None,
    }
    if automation_user_id and _conversation_supports_user_id():
        conversation_kwargs["user_id"] = automation_user_id
    conversation = Conversation(**conversation_kwargs)
    assert isinstance(conversation, RemoteConversation)
    print(f"  conversation created: {type(conversation).__name__}")
    print(f"  plugins loaded: {len(plugin_sources)}")
    if experiment_tags:
        print(f"  experiment tags: {experiment_tags}")

    # Inject secrets into the conversation (auto-exported as env vars in bash)
    if secrets:
        conversation.update_secrets(secrets)
        print(f"  injected {len(secrets)} secrets into conversation")

    # Build session URL from conversation ID and inject as a secret so
    # the agent can use $AUTOMATION_SESSION_URL in bash commands.
    if not IS_LOCAL_MODE and api_url:
        session_url = f"{api_url}/conversations/{conversation.id}"
        conversation.update_secrets({"AUTOMATION_SESSION_URL": session_url})
        print(f"  session URL: {session_url}")

    try:
        print(f"  sending prompt: {USER_PROMPT[:80]}...")
        conversation.send_message(USER_PROMPT)
        conversation.run()

        # Wait for the stream to settle
        while time.time() - last_event_time["ts"] < 2.0:
            time.sleep(0.1)

        cost = conversation.conversation_stats.get_combined_metrics().accumulated_cost
        print(f"  cost: {cost}")
        print(f"  events received: {len(received_events)}")
    finally:
        conversation.close()

    print("  conversation completed successfully")

    print("\n=== RESULT ===")
    print("ALL_OK")
