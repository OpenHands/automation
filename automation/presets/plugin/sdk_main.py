"""Plugin-based automation script — runs inside an OpenHands Cloud sandbox.

This script is auto-generated from a plugin automation request. It:
  1. Opens OpenHandsCloudWorkspace EARLY (ensures callback on any failure)
  2. Loads skills from cloned repos (if repos were configured)
  3. Fetches LLM config via workspace.get_llm()
  4. Fetches secrets via workspace.get_secrets()
  5. Fetches MCP config via workspace.get_mcp_config()
  6. Gets default agent with tools and condenser via get_default_agent()
  7. Loads plugins from plugins_config.json
  8. Creates a Conversation with all plugins and skills
  9. Sends the prompt (with event context if available) and runs
  10. On context manager exit, the workspace sends a completion callback

IMPORTANT: The workspace context is entered early so that ANY exception
(skill loading, plugin config parsing, etc.) triggers the __exit__ callback,
avoiding silent failures that require watchdog timeout.

Env vars injected by the dispatcher (read by the SDK automatically):
  OPENHANDS_API_KEY          - per-user automation API key
  OPENHANDS_CLOUD_API_URL    - SaaS API base URL
  SANDBOX_ID                 - this sandbox's Cloud API identifier
  SESSION_API_KEY            - session key for sandbox settings auth
  AUTOMATION_CALLBACK_URL    - completion callback endpoint (optional)
  AUTOMATION_RUN_ID          - run ID for the callback payload (optional)
  AUTOMATION_EVENT_PAYLOAD   - JSON with trigger info and event payload (optional)
"""

import json
import os
import sys
import time


api_key = os.environ.get("OPENHANDS_API_KEY", "")
api_url = os.environ.get("OPENHANDS_CLOUD_API_URL", "")
sandbox_id = os.environ.get("SANDBOX_ID", "")
session_key = os.environ.get("SESSION_API_KEY", "")

# Verify dispatcher-injected env vars (must happen before workspace context)
print("=== ENV VARS ===")
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
from openhands.workspace import OpenHandsCloudWorkspace

# Enter workspace context EARLY - any exception from here on triggers callback
print("\n=== SDK WORKSPACE ===")
with OpenHandsCloudWorkspace(
    local_agent_server_mode=True,
    cloud_api_url=api_url,
    cloud_api_key=api_key,
) as workspace:
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

    # Shared utility import (placed in tarball alongside this script)
    from load_skills import load_skills_from_repos

    # Load skills from cloned repos (if any)
    print("\n=== LOAD SKILLS ===")
    loaded_skills, agent_context = load_skills_from_repos()

    # Load configuration files
    SCRIPT_DIR = os.path.dirname(__file__)
    PLUGINS_CONFIG_FILE = os.path.join(SCRIPT_DIR, "plugins_config.json")
    PROMPT_FILE = os.path.join(SCRIPT_DIR, "prompt.txt")

    with open(PLUGINS_CONFIG_FILE) as f:
        plugins_config = json.load(f)

    with open(PROMPT_FILE) as f:
        USER_PROMPT = f.read()

    # If this is an event-triggered run, prepend event context to the prompt
    if event_context and "event" in event_context:
        event_json = json.dumps(event_context["event"], indent=2)
        USER_PROMPT = f"""This automation was triggered by a webhook event.

## Event Payload
```json
{event_json}
```

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
