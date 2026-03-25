"""Automation test script — runs inside an OpenHands Cloud sandbox.

Tests the full dispatch pipeline using the SDK:
  1. Verify env vars injected by the dispatcher
  2. Open OpenHandsCloudWorkspace with local_agent_server_mode=True
  3. Fetch LLM config via workspace.get_llm()
  4. Fetch secrets via workspace.get_secrets()
  5. Create a Conversation using the LLM and secrets
  6. Send a message and run the conversation
  7. On context manager exit, the workspace sends a completion callback

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

# 1. Verify dispatcher-injected env vars
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

print(f"  AUTOMATION_CALLBACK_URL: {os.environ.get('AUTOMATION_CALLBACK_URL') or 'NONE'}")
print(f"  AUTOMATION_RUN_ID: {os.environ.get('AUTOMATION_RUN_ID') or 'NONE'}")

# 2. Test SDK workspace in local_agent_server_mode
from openhands.sdk import Conversation, RemoteConversation  # noqa: E402
from openhands.tools.preset.default import get_default_agent  # noqa: E402
from openhands.workspace import OpenHandsCloudWorkspace  # noqa: E402


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

    # 3. Create an agent and conversation using the LLM and secrets
    print("\n=== CONVERSATION ===")
    agent = get_default_agent(llm=llm, cli_mode=True)

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

    # Build a prompt that exercises the injected secrets
    secret_names = list(secrets.keys()) if secrets else []
    if secret_names:
        names_str = ", ".join(f"${name}" for name in secret_names)
        prompt = (
            f"For each of these environment variables: {names_str} — "
            "print the variable name and the LAST 50% of its value "
            "(i.e. the second half of the string). "
            "Then write a short summary into SECRETS_CHECK.txt."
        )
    else:
        prompt = "Write 'Hello from automation!' into HELLO.txt"

    try:
        print(f"  sending prompt: {prompt[:80]}...")
        conversation.send_message(prompt)
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
