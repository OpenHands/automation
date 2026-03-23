"""Automation test script — runs inside an OpenHands Cloud sandbox.

Tests the full dispatch pipeline using the SDK:
  1. Verify env vars injected by the dispatcher
  2. Open OpenHandsCloudWorkspace with saas_runtime_mode=True
  3. Fetch LLM config via workspace.get_llm()
  4. Fetch secrets via workspace.get_secrets()
  5. On context manager exit, the workspace sends a completion callback

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

api_key = os.environ.get("OPENHANDS_API_KEY", "")
api_url = os.environ.get("OPENHANDS_CLOUD_API_URL", "")
sandbox_id = os.environ.get("SANDBOX_ID", "")
session_key = os.environ.get("SESSION_API_KEY", "")
callback_url = os.environ.get("AUTOMATION_CALLBACK_URL")
run_id = os.environ.get("AUTOMATION_RUN_ID")

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

print(f"  AUTOMATION_CALLBACK_URL: {callback_url or 'NONE'}")
print(f"  AUTOMATION_RUN_ID: {run_id or 'NONE'}")

# 2. Test SDK workspace in saas_runtime_mode
from openhands.workspace import OpenHandsCloudWorkspace  # noqa: E402

print("\n=== SDK WORKSPACE ===")
with OpenHandsCloudWorkspace(
    saas_runtime_mode=True,
    cloud_api_url=api_url,
    cloud_api_key=api_key,
    automation_callback_url=callback_url,
    automation_run_id=run_id,
) as workspace:
    # get_llm() — fetches LLM config from the user's SaaS account
    print("\n=== GET_LLM ===")
    llm = workspace.get_llm()
    print(f"  model: {llm.model}")
    print(f"  api_key present: {bool(llm.api_key)}")

    # get_secrets() — builds LookupSecret references for the user's secrets
    print("\n=== GET_SECRETS ===")
    try:
        secrets = workspace.get_secrets()
        print(f"  available: {list(secrets.keys()) or '(none)'}")
    except Exception as e:
        # Not a hard failure — user may not have secrets configured
        print(f"  get_secrets() failed (ok if no secrets): {e}")

print("\n=== RESULT ===")
print("ALL_OK")
