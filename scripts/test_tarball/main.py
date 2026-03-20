"""Example automation script — runs inside an OpenHands Cloud sandbox.

Mirrors the pattern proposed in the automations ADR (architecture PR #11)
and the SDK example (10_cloud_workspace_share_credentials.py):

  1. Open OpenHandsCloudWorkspace with the user's API key
     (creates an inner sandbox for the agent to work in)
  2. Fetch LLM config + secrets from the user's SaaS account
  3. Build an agent, start a conversation, run a task
  4. On context manager exit, cleanup the inner sandbox

Once SDK PR #2490 (saas_runtime_mode) merges, this script will switch to:
    OpenHandsCloudWorkspace(saas_runtime_mode=True, ...)
which skips creating an inner sandbox and uses the local agent-server
directly — the automation service's sandbox IS the workspace.

Env vars injected by the dispatcher:
  OPENHANDS_API_KEY          - per-user automation API key
  OPENHANDS_CLOUD_API_URL    - SaaS API base URL
  AUTOMATION_CALLBACK_URL    - completion callback endpoint (optional)
  AUTOMATION_RUN_ID          - run ID for the callback payload (optional)
"""

import os
import sys
import time

from openhands.sdk import Conversation, get_logger
from openhands.tools.preset.default import get_default_agent
from openhands.workspace import OpenHandsCloudWorkspace


logger = get_logger(__name__)

api_key = os.environ.get("OPENHANDS_API_KEY", "")
api_url = os.environ.get("OPENHANDS_CLOUD_API_URL", "https://app.all-hands.dev")
callback_url = os.environ.get("AUTOMATION_CALLBACK_URL")
run_id = os.environ.get("AUTOMATION_RUN_ID")

if not api_key:
    print("ERROR: OPENHANDS_API_KEY not set", file=sys.stderr)
    sys.exit(1)

print(f"API_URL={api_url}")
print(f"CALLBACK={callback_url or 'NONE'}")
print(f"RUN_ID={run_id or 'NONE'}")

with OpenHandsCloudWorkspace(
    cloud_api_url=api_url,
    cloud_api_key=api_key,
) as workspace:
    # Fetch LLM config from the user's SaaS account settings
    llm = workspace.get_llm()
    logger.info(f"LLM configured: model={llm.model}")

    # Fetch secrets (may be empty if no secrets configured)
    try:
        secrets = workspace.get_secrets()
        logger.info(f"Available secrets: {list(secrets.keys())}")
    except Exception as e:
        logger.warning(f"get_secrets() failed (no secrets configured?): {e}")
        secrets = {}

    # Build agent + conversation
    agent = get_default_agent(llm=llm, cli_mode=True)

    received_events: list = []
    last_event_time = {"ts": time.time()}

    def on_event(event) -> None:
        received_events.append(event)
        last_event_time["ts"] = time.time()

    conversation = Conversation(agent=agent, workspace=workspace, callbacks=[on_event])

    # Inject SaaS secrets into the conversation
    if secrets:
        conversation.update_secrets(secrets)
        logger.info(f"Injected {len(secrets)} secrets")

    # Run a simple task
    conversation.send_message("Write 'hello world' to /tmp/automation_test.txt")
    conversation.run()

    # Wait for trailing events
    while time.time() - last_event_time["ts"] < 2.0:
        time.sleep(0.1)

    conversation.close()
    logger.info(f"Conversation done — {len(received_events)} events")

print("ALL_OK")
