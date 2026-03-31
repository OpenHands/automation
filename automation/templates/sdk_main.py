"""Prompt-based automation script — runs inside an OpenHands Cloud sandbox.

This script is auto-generated from a user's prompt. It:
  1. Opens OpenHandsCloudWorkspace with local_agent_server_mode=True
  2. Fetches LLM config via workspace.get_llm()
  3. Fetches secrets via workspace.get_secrets()
  4. Fetches MCP config via workspace.get_mcp_config()
  5. Creates a LLMSummarizingCondenser to manage context
  6. Creates an Agent with LLM, MCP config, and condenser
  7. Creates a Conversation and injects secrets
  8. Sends the user's prompt and runs the conversation
  9. On context manager exit, the workspace sends a completion callback

Env vars injected by the dispatcher (read by the SDK automatically):
  OPENHANDS_API_KEY          - per-user automation API key
  OPENHANDS_CLOUD_API_URL    - SaaS API base URL
  SANDBOX_ID                 - this sandbox's Cloud API identifier
  SESSION_API_KEY            - session key for sandbox settings auth
  AUTOMATION_CALLBACK_URL    - completion callback endpoint (optional)
  AUTOMATION_RUN_ID          - run ID for the callback payload (optional)
"""

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

# SDK imports
from openhands.sdk import Agent, Conversation, RemoteConversation, Tool
from openhands.sdk.context.condenser import LLMSummarizingCondenser
from openhands.tools.file_editor import FileEditorTool
from openhands.tools.terminal import TerminalTool
from openhands.workspace import OpenHandsCloudWorkspace


# Load user's prompt from file (placed during automation creation)
PROMPT_FILE = os.path.join(os.path.dirname(__file__), "prompt.txt")
with open(PROMPT_FILE) as f:
    USER_PROMPT = f.read()


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

    # Create context condenser to manage conversation history
    print("\n=== CONDENSER ===")
    condenser = LLMSummarizingCondenser(
        llm=llm.model_copy(update={"usage_id": "condenser"}),
        max_size=100,
        keep_first=2,
    )
    print("  LLMSummarizingCondenser configured (max_size=100, keep_first=2)")

    # Create agent with tools, MCP config, and condenser
    print("\n=== AGENT ===")
    tools = [
        Tool(name=TerminalTool.name),
        Tool(name=FileEditorTool.name),
    ]
    agent = Agent(
        llm=llm,
        tools=tools,
        mcp_config=mcp_config,
        condenser=condenser,
    )
    print(f"  tools: {[t.name for t in tools]}")
    print(f"  mcp_config: {'configured' if mcp_config else 'none'}")
    print("  condenser: LLMSummarizingCondenser")

    # Create conversation
    print("\n=== CONVERSATION ===")
    received_events: list = []
    last_event_time = {"ts": time.time()}

    def event_callback(event) -> None:
        received_events.append(event)
        last_event_time["ts"] = time.time()

    conversation = Conversation(
        agent=agent, workspace=workspace, callbacks=[event_callback]
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
