"""Plugin-based automation script — runs inside an OpenHands execution environment.

This script is auto-generated from a plugin automation request. It supports two modes:

**Cloud Mode** (default):
  Uses Workspace connected to the sandbox's agent server with Cloud API features.
  Requires: OPENHANDS_API_KEY, OPENHANDS_CLOUD_API_URL, SANDBOX_ID, SESSION_API_KEY
  LLM/secrets/MCP fetched from user's Cloud account via agent server settings API.

**Local Mode** (self-hosted):
  Uses Workspace connected to a local agent server.
  Requires: AGENT_SERVER_URL (presence triggers local mode)
  LLM/secrets configured via agent server's settings API (pre-configured by admin).

The script:
  1. Detects mode based on AGENT_SERVER_URL presence
  2. Creates a remote Workspace connected to the agent server
  3. Gets LLM config from agent server settings API
  4. Gets secrets from agent server settings API (if configured)
  5. Gets MCP config from agent server settings API (if configured)
  6. Gets default agent with tools and condenser
  7. Loads plugins from plugins_config.json
  8. Creates a RemoteConversation with all plugins
  9. Sends the prompt (with event context if available) and runs

Env vars (Cloud mode - all required):
  OPENHANDS_API_KEY          - per-user automation API key
  OPENHANDS_CLOUD_API_URL    - SaaS API base URL
  SANDBOX_ID                 - this sandbox's Cloud API identifier
  SESSION_API_KEY            - session key for sandbox settings auth

Env vars (Local mode):
  AGENT_SERVER_URL           - local agent server URL (presence = local mode)
  SESSION_API_KEY            - API key for agent server auth (optional)

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
from pydantic import SecretStr


# Detect execution mode based on AGENT_SERVER_URL presence
agent_server_url = os.environ.get("AGENT_SERVER_URL", "").rstrip("/")
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
    # Local mode: AGENT_SERVER_URL required, LLM config from agent server settings
    print(f"  AGENT_SERVER_URL: {'OK' if agent_server_url else 'MISSING'}")
    print(f"  SESSION_API_KEY: {'OK' if session_key else 'NONE (may fail auth)'}")
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

# SDK imports
from openhands.sdk import Conversation, LLM, RemoteConversation, Workspace
from openhands.sdk.plugin import PluginSource
from openhands.tools.preset.default import get_default_agent

# Determine agent server URL
# In local mode, use AGENT_SERVER_URL directly
# In Cloud mode, agent server runs on localhost:3000 inside the sandbox
if IS_LOCAL_MODE:
    server_url = agent_server_url
else:
    server_url = "http://localhost:3000"

print("\n=== SDK WORKSPACE ===")
print(f"  connecting to agent server: {server_url}")

# Create remote workspace connected to the agent server
workspace = Workspace(host=server_url)

# Verify connectivity
result = workspace.execute_command("pwd")
print(f"  workspace ready, cwd: {result.stdout.strip()}")

# Parse event payload if present (for event-triggered automations)
event_context = None
if event_payload_json := os.environ.get("AUTOMATION_EVENT_PAYLOAD"):
    try:
        event_context = json.loads(event_payload_json)
    except json.JSONDecodeError as e:
        print(f"ERROR: Failed to parse AUTOMATION_EVENT_PAYLOAD: {e}", file=sys.stderr)

# Load configuration files
SCRIPT_DIR = os.path.dirname(__file__)
PLUGINS_CONFIG_FILE = os.path.join(SCRIPT_DIR, "plugins_config.json")
PROMPT_FILE = os.path.join(SCRIPT_DIR, "prompt.txt")

with open(PLUGINS_CONFIG_FILE) as f:
    plugins_config = json.load(f)

with open(PROMPT_FILE) as f:
    USER_PROMPT = f.read()

# Build prompt with context sections
context_sections = []

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

# Get LLM config from agent server settings API
print("\n=== GET_LLM ===")
llm = None
try:
    with httpx.Client(base_url=server_url, timeout=30.0) as client:
        response = client.get("/api/settings/llm")
        if response.status_code == 200:
            llm_config = response.json()
            llm = LLM(
                model=llm_config.get("model", "anthropic/claude-sonnet-4-20250514"),
                api_key=SecretStr(llm_config["api_key"]) if llm_config.get("api_key") else None,
                base_url=llm_config.get("base_url"),
            )
            print(f"  model: {llm.model} (from agent server settings)")
        else:
            print(f"  warning: GET /api/settings/llm returned {response.status_code}")
except Exception as e:
    print(f"  warning: failed to get LLM config from agent server: {e}")

if llm is None:
    print("FAIL: Could not get LLM configuration from agent server", file=sys.stderr)
    sys.exit(1)

print(f"  api_key present: {bool(llm.api_key)}")

# Get secrets from agent server settings API
print("\n=== GET_SECRETS ===")
secrets = {}
try:
    with httpx.Client(base_url=server_url, timeout=30.0) as client:
        response = client.get("/api/settings/secrets")
        if response.status_code == 200:
            secrets_data = response.json()
            # Build LookupSecret references for each secret
            for secret_info in secrets_data.get("secrets", []):
                secret_name = secret_info["name"]
                secrets[secret_name] = {
                    "kind": "LookupSecret",
                    "url": f"{server_url}/api/settings/secrets/{secret_name}",
                }
            print(f"  available: {list(secrets.keys()) or '(none)'}")
        else:
            print(f"  warning: GET /api/settings/secrets returned {response.status_code}")
except Exception as e:
    print(f"  warning: failed to get secrets: {e}")

# Get MCP config from agent server settings API (if configured)
print("\n=== GET_MCP_CONFIG ===")
mcp_config = None
try:
    with httpx.Client(base_url=server_url, timeout=30.0) as client:
        response = client.get("/api/settings/mcp")
        if response.status_code == 200:
            mcp_config = response.json()
            if mcp_config and mcp_config.get("mcpServers"):
                print(f"  servers: {list(mcp_config['mcpServers'].keys())}")
            else:
                print("  no MCP servers configured")
        else:
            print(f"  warning: GET /api/settings/mcp returned {response.status_code}")
except Exception as e:
    print(f"  skipped (not configured or not available): {e}")

# Get default agent with tools and condenser (CLI mode to disable browser)
print("\n=== AGENT ===")
agent = get_default_agent(llm=llm, cli_mode=True)

# Add MCP config if configured
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

# Inject secrets into the conversation (as LookupSecret references)
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
