"""Plugin-based automation script — runs inside an OpenHands execution environment.

This script is auto-generated from a plugin automation request. It supports two modes:

**Cloud Mode** (default):
  Uses OpenHandsCloudWorkspace connected to OpenHands Cloud API.
  Requires: OPENHANDS_API_KEY, OPENHANDS_CLOUD_API_URL, SANDBOX_ID, SESSION_API_KEY

**Local Mode** (self-hosted):
  Uses RemoteWorkspace connected to a local agent server.
  Requires: AGENT_SERVER_URL (presence triggers local mode)
  Optional: OPENHANDS_CLOUD_API_URL for LLM/secrets/MCP config via Cloud API

The script:
  1. Detects mode based on AGENT_SERVER_URL presence
  2. Opens the appropriate workspace EARLY (ensures callback on any failure)
  3. Clones repositories if configured (via workspace.clone_repos())
  4. Loads ALL skills via workspace.load_skills_from_agent_server()
  5. Fetches LLM config via workspace.get_llm()
  6. Fetches secrets via workspace.get_secrets()
  7. Fetches MCP config via workspace.get_mcp_config()
  8. Gets default agent with tools and condenser via get_default_agent()
  9. Loads plugins from plugins_config.json
  10. Creates a Conversation with all plugins and skills
  11. Sends the prompt (with event context if available) and runs
  12. On context manager exit, the workspace sends a completion callback

IMPORTANT: The workspace context is entered early so that ANY exception
(skill loading, plugin config parsing, etc.) triggers the __exit__ callback,
avoiding silent failures that require watchdog timeout.

Env vars (Cloud mode - all required):
  OPENHANDS_API_KEY          - per-user automation API key
  OPENHANDS_CLOUD_API_URL    - SaaS API base URL
  SANDBOX_ID                 - this sandbox's Cloud API identifier
  SESSION_API_KEY            - session key for sandbox settings auth

Env vars (Local mode):
  AGENT_SERVER_URL           - local agent server URL (presence = local mode)
  OPENHANDS_CLOUD_API_URL    - optional, for LLM/secrets/MCP via Cloud API
  OPENHANDS_API_KEY          - optional, required if using Cloud API features

Common env vars:
  AUTOMATION_CALLBACK_URL    - completion callback endpoint (optional)
  AUTOMATION_RUN_ID          - run ID for the callback payload (optional)
  AUTOMATION_EVENT_PAYLOAD   - JSON with trigger info and event payload (optional)
"""

import json
import os
import sys
import time


# Detect execution mode based on AGENT_SERVER_URL presence
agent_server_url = os.environ.get("AGENT_SERVER_URL", "")
IS_LOCAL_MODE = bool(agent_server_url)

# Cloud mode env vars
api_key = os.environ.get("OPENHANDS_API_KEY", "")
api_url = os.environ.get("OPENHANDS_CLOUD_API_URL", "")
sandbox_id = os.environ.get("SANDBOX_ID", "")
session_key = os.environ.get("SESSION_API_KEY", "")

print("=== EXECUTION MODE ===")
print(f"  mode: {'LOCAL' if IS_LOCAL_MODE else 'CLOUD'}")

print("\n=== ENV VARS ===")
if IS_LOCAL_MODE:
    # Local mode: only AGENT_SERVER_URL is required
    print(f"  AGENT_SERVER_URL: {'OK' if agent_server_url else 'MISSING'}")
    print(f"  OPENHANDS_CLOUD_API_URL: {'OK' if api_url else 'NONE (no Cloud features)'}")
    print(f"  OPENHANDS_API_KEY: {'OK' if api_key else 'NONE (no Cloud features)'}")
    if not agent_server_url:
        print("FAIL: AGENT_SERVER_URL not set for local mode", file=sys.stderr)
        sys.exit(1)
else:
    # Cloud mode: all Cloud env vars are required
    for name, val in [
        ("OPENHANDS_API_KEY", api_key),
        ("OPENHANDS_CLOUD_API_URL", api_url),
        ("SANDBOX_ID", sandbox_id),
        ("SESSION_API_KEY", session_key),
    ]:
        print(f"  {name}: {'OK' if val else 'MISSING'}")
        if not val:
            print(f"FAIL: {name} not set", file=sys.stderr)
            sys.exit(1)

print(
    f"  AUTOMATION_CALLBACK_URL: {os.environ.get('AUTOMATION_CALLBACK_URL') or 'NONE'}"
)
print(f"  AUTOMATION_RUN_ID: {os.environ.get('AUTOMATION_RUN_ID') or 'NONE'}")

# SDK imports (before workspace context so import errors are caught)
from openhands.sdk import Conversation, RemoteConversation
from openhands.sdk.plugin import PluginSource
from openhands.tools import get_default_agent

if IS_LOCAL_MODE:
    from openhands.workspace import RemoteWorkspace
else:
    from openhands.workspace import OpenHandsCloudWorkspace

# Create workspace based on mode
print("\n=== SDK WORKSPACE ===")
if IS_LOCAL_MODE:
    print(f"  using RemoteWorkspace at {agent_server_url}")
    workspace_ctx = RemoteWorkspace(
        agent_server_url=agent_server_url,
        # Optional Cloud API integration for LLM/secrets/MCP
        cloud_api_url=api_url if api_url else None,
        cloud_api_key=api_key if api_key else None,
    )
else:
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

    # Clone repositories if repos_config.json exists (uses SDK workspace methods)
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
    PLUGINS_CONFIG_FILE = os.path.join(SCRIPT_DIR, "plugins_config.json")
    PROMPT_FILE = os.path.join(SCRIPT_DIR, "prompt.txt")

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

    # get_llm() — fetches LLM config from the user's SaaS account
    print("\n=== GET_LLM ===")
    llm = workspace.get_llm()
    print(f"  model: {llm.model}")
    print(f"  api_key present: {bool(llm.api_key)}")

    # get_secrets() — builds LookupSecret references for the user's secrets
    print("\n=== GET_SECRETS ===")
    secrets = {}
    try:
        secrets = workspace.get_secrets()
        print(f"  available: {list(secrets.keys()) or '(none)'}")
    except Exception as e:
        # Not a hard failure — user may not have secrets configured
        print(f"  get_secrets() failed (ok if no secrets): {e}")

    # get_mcp_config() — fetches MCP server configuration from user's account
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

    conversation = Conversation(
        agent=agent,
        workspace=workspace,
        plugins=plugin_sources,  # All plugins loaded here
        callbacks=[event_callback],
        delete_on_close=False,  # Keep conversation visible in GUI after completion
    )
    assert isinstance(conversation, RemoteConversation)
    print(f"  conversation created: {type(conversation).__name__}")
    print(f"  plugins loaded: {len(plugin_sources)}")

    # Inject SaaS secrets into the conversation
    if secrets:
        conversation.update_secrets(secrets)
        print(f"  injected {len(secrets)} secrets into conversation")

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
