"""Prompt-based automation script — runs inside an OpenHands execution environment.

This script is auto-generated from a user's prompt. It supports two modes:

**Cloud Mode** (default):
  Uses OpenHandsCloudWorkspace connected to OpenHands Cloud API.
  Requires: OPENHANDS_API_KEY, OPENHANDS_CLOUD_API_URL, SANDBOX_ID, SESSION_API_KEY
  LLM/secrets/MCP fetched from user's Cloud account via workspace methods.

**Local Mode** (self-hosted):
  Uses OpenHandsCloudWorkspace with local_agent_server_mode=True.
  Requires: AGENT_SERVER_URL (presence triggers local mode)
  LLM/secrets/MCP fetched from agent-server's persisted settings (via /api/settings).
  Falls back to env vars: LLM_MODEL, LLM_API_KEY, LLM_BASE_URL if not configured.

The script:
  1. Detects mode based on AGENT_SERVER_URL presence
  2. Opens the appropriate workspace EARLY (ensures callback on any failure)
  3. Clones repositories if configured (via workspace.clone_repos())
  4. Loads ALL skills via workspace.load_skills_from_agent_server()
  5. Gets LLM config (Cloud: workspace.get_llm(), Local: agent-server settings or env)
  6. Gets secrets if available (both modes via agent-server or Cloud API)
  7. Gets MCP config if available (both modes via agent-server or Cloud API)
  8. Gets default agent with tools and condenser via get_default_agent()
  9. Creates a Conversation and injects secrets
  10. Sends the user's prompt (with event context if available) and runs
  11. On context manager exit, the workspace sends a completion callback

IMPORTANT: The workspace context is entered early so that ANY exception
(skill loading, prompt parsing, etc.) triggers the __exit__ callback,
avoiding silent failures that require watchdog timeout.

Env vars (Cloud mode - all required):
  OPENHANDS_API_KEY          - per-user automation API key
  OPENHANDS_CLOUD_API_URL    - SaaS API base URL
  SANDBOX_ID                 - this sandbox's Cloud API identifier
  SESSION_API_KEY            - session key for sandbox settings auth

Env vars (Local mode):
  AGENT_SERVER_URL           - local agent server URL (presence = local mode)
  SESSION_API_KEY            - API key for agent server auth (optional)
  LLM_MODEL                  - fallback LLM model if not in saved settings
  LLM_API_KEY                - fallback LLM API key if not in saved settings
  LLM_BASE_URL               - fallback LLM base URL if not in saved settings

Common env vars:
  AUTOMATION_CALLBACK_URL    - completion callback endpoint (optional)
  AUTOMATION_RUN_ID          - run ID for the callback payload (optional)
  AUTOMATION_EVENT_PAYLOAD   - JSON with trigger info and event payload (optional)
"""

import json
import os
import sys
import time

import httpx


def fetch_agent_server_settings(base_url: str, api_key: str | None = None) -> dict:
    """Fetch settings from agent-server's /api/settings endpoint."""
    headers = {}
    if api_key:
        headers["X-Session-API-Key"] = api_key

    try:
        response = httpx.get(f"{base_url.rstrip('/')}/api/settings", headers=headers, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"  Warning: Failed to fetch settings from agent-server: {e}")
        return {}


def fetch_agent_server_secrets(base_url: str, api_key: str | None = None) -> dict:
    """Fetch secrets list from agent-server and resolve values."""
    headers = {}
    if api_key:
        headers["X-Session-API-Key"] = api_key

    secrets = {}
    try:
        # Get list of secrets
        response = httpx.get(f"{base_url.rstrip('/')}/api/settings/secrets", headers=headers, timeout=10)
        response.raise_for_status()
        secrets_list = response.json().get("secrets", [])

        # Fetch each secret value
        for secret_info in secrets_list:
            name = secret_info.get("name")
            if name:
                try:
                    val_response = httpx.get(
                        f"{base_url.rstrip('/')}/api/settings/secrets/{name}",
                        headers=headers,
                        timeout=10
                    )
                    val_response.raise_for_status()
                    secrets[name] = val_response.text
                except Exception:
                    pass
    except Exception as e:
        print(f"  Warning: Failed to fetch secrets from agent-server: {e}")

    return secrets


# Detect execution mode based on AGENT_SERVER_URL presence
agent_server_url = os.environ.get("AGENT_SERVER_URL", "")
IS_LOCAL_MODE = bool(agent_server_url)

# Cloud mode env vars
api_key = os.environ.get("OPENHANDS_API_KEY", "")
api_url = os.environ.get("OPENHANDS_CLOUD_API_URL", "")
sandbox_id = os.environ.get("SANDBOX_ID", "")
session_key = os.environ.get("SESSION_API_KEY", "")

# Local mode env vars (fallbacks if not in saved settings)
llm_model_env = os.environ.get("LLM_MODEL", "")
llm_api_key_env = os.environ.get("LLM_API_KEY", "")
llm_base_url_env = os.environ.get("LLM_BASE_URL", "")

print("=== EXECUTION MODE ===")
print(f"  mode: {'LOCAL' if IS_LOCAL_MODE else 'CLOUD'}")

print("\n=== ENV VARS ===")
if IS_LOCAL_MODE:
    # Local mode: AGENT_SERVER_URL required; LLM can come from saved settings or env
    print(f"  AGENT_SERVER_URL: {'OK' if agent_server_url else 'MISSING'}")
    print(f"  SESSION_API_KEY: {'OK' if session_key else 'NONE (may fail auth)'}")
    print(f"  LLM_MODEL: {'OK (env fallback)' if llm_model_env else 'NONE (will use saved settings)'}")
    print(f"  LLM_API_KEY: {'OK (env fallback)' if llm_api_key_env else 'NONE (will use saved settings)'}")
    print(f"  LLM_BASE_URL: {'OK (env fallback)' if llm_base_url_env else 'NONE'}")
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
from openhands.sdk import Conversation, LLM, RemoteConversation
from openhands.tools import get_default_agent
from openhands.workspace import OpenHandsCloudWorkspace

# Create workspace based on mode
# Both modes use OpenHandsCloudWorkspace with local_agent_server_mode=True
# This provides full workspace functionality (skills, commands, etc.)
print("\n=== SDK WORKSPACE ===")
if IS_LOCAL_MODE:
    # Local mode: connect to local agent server with Cloud API features disabled
    # Extract port from agent_server_url (e.g., "http://localhost:3000" -> 3000)
    port = 3000
    if ":" in agent_server_url:
        port_str = agent_server_url.rsplit(":", 1)[-1].rstrip("/")
        if port_str.isdigit():
            port = int(port_str)
    print(f"  using OpenHandsCloudWorkspace (local mode) on port {port}")
    # Use SESSION_API_KEY for callback auth (auth is disabled in local mode,
    # but the SDK still sends a Bearer token, so we need a non-empty value)
    workspace_ctx = OpenHandsCloudWorkspace(
        local_agent_server_mode=True,
        agent_server_port=port,
        # Empty URL (not used in local mode), but provide a key for callback auth
        cloud_api_url="",
        cloud_api_key=session_key or "local-mode",
    )
else:
    # Cloud mode: connect to sandbox's agent server with full Cloud API features
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

    # Load user's prompt from file (placed during automation creation)
    PROMPT_FILE = os.path.join(os.path.dirname(__file__), "prompt.txt")
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

    # Get LLM config - different approach for local vs Cloud mode
    print("\n=== GET_LLM ===")
    if IS_LOCAL_MODE:
        # Local mode: try to get LLM from agent-server settings, fall back to env vars
        saved_settings = fetch_agent_server_settings(agent_server_url, session_key)
        agent_settings = saved_settings.get("agent_settings", {})
        llm_settings = agent_settings.get("llm", {})

        # Extract LLM config from saved settings or use env var fallbacks
        llm_model = llm_settings.get("model") or llm_model_env
        llm_api_key = llm_settings.get("api_key") or llm_api_key_env
        llm_base_url = llm_settings.get("base_url") or llm_base_url_env

        if not llm_model or not llm_api_key:
            print("FAIL: LLM_MODEL and LLM_API_KEY required (not in saved settings or env)", file=sys.stderr)
            sys.exit(1)

        llm = LLM(
            model=llm_model,
            api_key=llm_api_key,
            base_url=llm_base_url if llm_base_url else None,
        )
        source = "saved settings" if llm_settings.get("model") else "env vars"
        print(f"  model: {llm.model} (from {source})")
    else:
        # Cloud mode: fetch from user's SaaS account
        llm = workspace.get_llm()
        print(f"  model: {llm.model} (from Cloud API)")
    print(f"  api_key present: {bool(llm.api_key)}")

    # get_secrets() — fetch from agent-server in local mode, Cloud API in cloud mode
    print("\n=== GET_SECRETS ===")
    secrets = {}
    if IS_LOCAL_MODE:
        # Local mode: fetch secrets from agent-server
        secrets = fetch_agent_server_secrets(agent_server_url, session_key)
        if secrets:
            print(f"  available: {list(secrets.keys())}")
        else:
            print("  no secrets configured (or failed to fetch)")
    else:
        try:
            secrets = workspace.get_secrets()
            print(f"  available: {list(secrets.keys()) or '(none)'}")
        except Exception as e:
            # Not a hard failure — user may not have secrets configured
            print(f"  get_secrets() failed (ok if no secrets): {e}")

    # get_mcp_config() — fetch from saved settings in local mode, Cloud API in cloud mode
    print("\n=== GET_MCP_CONFIG ===")
    mcp_config = None
    if IS_LOCAL_MODE:
        # Local mode: extract MCP config from agent-server settings
        mcp_config = agent_settings.get("mcp_config")
        if mcp_config and (mcp_config.get("sse_servers") or mcp_config.get("stdio_servers") or mcp_config.get("shttp_servers")):
            server_count = len(mcp_config.get("sse_servers", [])) + len(mcp_config.get("stdio_servers", [])) + len(mcp_config.get("shttp_servers", []))
            print(f"  servers: {server_count} configured (from saved settings)")
        else:
            print("  no MCP servers configured")
            mcp_config = None
    else:
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

    # Create conversation
    print("\n=== CONVERSATION ===")
    received_events: list = []
    last_event_time = {"ts": time.time()}

    def event_callback(event) -> None:
        received_events.append(event)
        last_event_time["ts"] = time.time()

    conversation = Conversation(
        agent=agent,
        workspace=workspace,
        callbacks=[event_callback],
        delete_on_close=False,  # Keep conversation visible in GUI after completion
    )
    assert isinstance(conversation, RemoteConversation)
    print(f"  conversation created: {type(conversation).__name__}")

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
