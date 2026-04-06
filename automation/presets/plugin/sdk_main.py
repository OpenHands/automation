"""Plugin-based automation script — runs inside an OpenHands Cloud sandbox.

This script is auto-generated from a plugin automation request. It:
  1. Opens OpenHandsCloudWorkspace with local_agent_server_mode=True
  2. Fetches LLM config via workspace.get_llm()
  3. Fetches secrets via workspace.get_secrets()
  4. Fetches MCP config via workspace.get_mcp_config()
  5. Gets default agent with tools and condenser via get_default_agent()
  6. Loads plugins from plugins_config.json
  7. Creates a Conversation with all plugins
  8. Sends the prompt (with event context if available) and runs
  9. On context manager exit, the workspace sends a completion callback

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

# Verify dispatcher-injected env vars
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

# Parse event payload if present (for event-triggered automations)
event_payload_json = os.environ.get("AUTOMATION_EVENT_PAYLOAD", "")
event_context = None
if event_payload_json:
    try:
        event_context = json.loads(event_payload_json)
        print(f"  AUTOMATION_EVENT_PAYLOAD: present")
        if "event" in event_context:
            print(f"    trigger_type: {event_context.get('trigger', {}).get('type')}")
            print(f"    event_key: {event_context['event'].get('event_key', 'N/A')}")
    except json.JSONDecodeError as e:
        print(f"  AUTOMATION_EVENT_PAYLOAD: invalid JSON ({e})")
else:
    print(f"  AUTOMATION_EVENT_PAYLOAD: NONE")

# SDK imports
from openhands.sdk import Conversation, RemoteConversation
from openhands.sdk.plugin import PluginSource
from openhands.tools import get_default_agent
from openhands.workspace import OpenHandsCloudWorkspace


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
    event_data = event_context["event"]
    event_json = json.dumps(event_data, indent=2)
    USER_PROMPT = f"""This automation was triggered by a webhook event.

## Event Payload
```json
{event_json}
```

## Task
{USER_PROMPT}"""
    print("\n=== EVENT-TRIGGERED ===")
    print(f"  Event context prepended to prompt")

# Deserialize plugin sources using Pydantic validation
plugin_sources = [PluginSource.model_validate(p) for p in plugins_config]

print("\n=== PLUGINS CONFIG ===")
print(f"  loading {len(plugin_sources)} plugin(s):")
for ps in plugin_sources:
    ref_str = f"@{ps.ref}" if ps.ref else ""
    path_str = f" ({ps.repo_path})" if ps.repo_path else ""
    print(f"    - {ps.source}{ref_str}{path_str}")


print("\n=== SDK WORKSPACE ===")
with OpenHandsCloudWorkspace(
    local_agent_server_mode=True,
    cloud_api_url=api_url,
    cloud_api_key=api_key,
) as workspace:
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

    # Add user's MCP config using model_copy if configured
    # (Plugin MCP configs will be merged when plugins are loaded)
    if mcp_config:
        agent = agent.model_copy(update={"mcp_config": mcp_config})

    print(f"  tools: {[t.name for t in agent.tools]}")
    print(f"  mcp_config: {'configured' if mcp_config else 'none'}")
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
