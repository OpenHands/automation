"""Plugin-spec conversation runner — runs inside an OpenHands execution environment.

This script is auto-generated from a plugin automation manifest whose entrypoint
uses the stock conversation runner. Plugin-spec conversation runs are launched
through an Agent Profile selected by the Automation Service from the plugin
manifest's runtime.agent_profile requirement.

The script:
  1. Detects mode based on AGENT_SERVER_URL presence.
  2. Opens the workspace context early so failures trigger the completion callback.
  3. Loads skills already available to the agent server.
  4. Reads plugin sources, prompt text, and event context.
  5. Creates a RemoteConversation through StartConversationRequest with the
     selected agent_profile_id and all plugin sources.
  6. Sends the assembled prompt and runs the conversation.
  7. On context manager exit, the workspace sends a completion callback.

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
  AUTOMATION_CALLBACK_URL     - completion callback endpoint (optional)
  AUTOMATION_ID               - automation ID for observability correlation (optional)
  AUTOMATION_NAME             - automation name for observability correlation (optional)
  AUTOMATION_RUN_ID           - run ID for the callback payload (optional)
  AUTOMATION_USER_ID          - owner user ID for observability attribution (optional)
  AUTOMATION_ORG_ID           - owner org ID for observability context (optional)
  AUTOMATION_TRIGGER_SOURCE   - trigger source for observability context (optional)
  AUTOMATION_EVENT_PAYLOAD    - JSON with trigger info and event payload (optional)
  AUTOMATION_AGENT_PROFILE_ID - required selected Agent Profile id
  AUTOMATION_AGENT_PROFILE_NAME - selected Agent Profile name

Runtime-injected secrets (via conversation.update_secrets after creation):
  AUTOMATION_SESSION_URL      - direct URL to this conversation in Agent Canvas
                                (Cloud mode only; built from conversation.id)
"""

import inspect
import json
import os
import sys
import uuid

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
session_key = os.environ.get("OH_SESSION_API_KEYS_0") or os.environ.get(
    "SESSION_API_KEY", ""
)
agent_profile_id = os.environ.get("AUTOMATION_AGENT_PROFILE_ID") or None
agent_profile_name = os.environ.get("AUTOMATION_AGENT_PROFILE_NAME") or None
agent_profile_kind = os.environ.get("AUTOMATION_AGENT_PROFILE_KIND") or None
automation_user_id = os.environ.get("AUTOMATION_USER_ID") or None

print("=== EXECUTION MODE ===")
print(f"  mode: {'LOCAL' if IS_LOCAL_MODE else 'CLOUD'}")

print("\n=== ENV VARS ===")
if IS_LOCAL_MODE:
    # Local mode: AGENT_SERVER_URL required
    print(f"  AGENT_SERVER_URL: {'OK' if agent_server_url else 'MISSING'}")
    print(f"  OH_SESSION_API_KEYS_0: {'OK' if session_key else 'NONE (may fail auth)'}")
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
print(f"  AUTOMATION_USER_ID: {'OK' if automation_user_id else 'NONE'}")
print(f"  AUTOMATION_ORG_ID: {'OK' if os.environ.get('AUTOMATION_ORG_ID') else 'NONE'}")
print(f"  AUTOMATION_RUN_ID: {os.environ.get('AUTOMATION_RUN_ID') or 'NONE'}")

# SDK imports (before workspace context so import errors are caught)
from openhands.sdk import Conversation, RemoteConversation
from openhands.sdk.automation import automation_conversation_kwargs
from openhands.sdk.conversation.request import StartConversationRequest
from openhands.sdk.workspace.local import LocalWorkspace

from openhands.sdk.plugin import PluginSource
from prompt_builder import build_user_prompt
from openhands.sdk.workspace.remote.base import RemoteWorkspace
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
        keep_alive=True,
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

    SCRIPT_DIR = os.path.dirname(__file__)

    print("\n=== LOAD SKILLS ===")
    loaded_skills, _agent_context = workspace.load_skills_from_agent_server()
    print(f"  loaded {len(loaded_skills)} skills")

    # Load configuration files
    PLUGINS_CONFIG_FILE = os.path.join(SCRIPT_DIR, "plugins_config.json")
    PROMPT_FILE = os.path.join(SCRIPT_DIR, "prompt.txt")

    with open(PLUGINS_CONFIG_FILE) as f:
        plugins_config = json.load(f)

    with open(PROMPT_FILE) as f:
        USER_PROMPT = build_user_prompt(f.read(), event_context)

    # Deserialize plugin sources using Pydantic validation
    plugin_sources = [PluginSource.model_validate(p) for p in plugins_config]

    print("\n=== PLUGINS CONFIG ===")
    print(f"  loading {len(plugin_sources)} plugin(s):")
    for ps in plugin_sources:
        ref_str = f"@{ps.ref}" if ps.ref else ""
        path_str = f" ({ps.repo_path})" if ps.repo_path else ""
        print(f"    - {ps.source}{ref_str}{path_str}")

    # Get secrets via workspace. The agent-server applies the selected
    # profile's secret_refs when agent_profile_id is present.
    print("\n=== GET_SECRETS ===")
    secrets = {}
    try:
        secrets = workspace.get_secrets(agent_profile_id=agent_profile_id)
        print(f"  profile: {agent_profile_name or agent_profile_id or 'DEFAULT'}")
        print(f"  available: {list(secrets.keys()) or '(none)'}")
    except Exception as e:
        # Not a hard failure — user may not have secrets configured
        print(f"  get_secrets() failed (ok if no secrets): {e}")

    if not agent_profile_id:
        raise RuntimeError("Plugin-spec conversation runner requires an agent profile")

    print("\n=== AGENT PROFILE ===")
    print(f"  profile id: {agent_profile_id}")
    print(f"  profile name: {agent_profile_name or 'UNKNOWN'}")
    print(f"  profile kind: {agent_profile_kind or 'UNKNOWN'}")

    # Create conversation with plugins
    print("\n=== CONVERSATION ===")

    conversation_kwargs = {
        "workspace": workspace,
        "plugins": plugin_sources,  # All plugins loaded here
        **automation_conversation_kwargs(),
    }
    if automation_user_id and _conversation_supports_user_id():
        conversation_kwargs["user_id"] = automation_user_id
    # A `continue_conversation` run must use the derived id: RemoteConversation
    # attaches to that conversation if it exists, and creates it if it does not.
    automation_conversation_id = os.environ.get("AUTOMATION_CONVERSATION_ID")
    if automation_conversation_id:
        conversation_kwargs["conversation_id"] = uuid.UUID(automation_conversation_id)

    request_kwargs = {
        **conversation_kwargs,
        "workspace": LocalWorkspace(working_dir=workspace.working_dir),
        "agent_profile_id": uuid.UUID(agent_profile_id),
    }
    conversation = RemoteConversation.create(
        workspace, StartConversationRequest(**request_kwargs)
    )
    assert isinstance(conversation, RemoteConversation)
    print(f"  conversation created: {type(conversation).__name__}")
    print(f"  plugins loaded: {len(plugin_sources)}")

    # Inject secrets into the conversation (auto-exported as env vars in bash)
    if secrets:
        conversation.update_secrets(secrets)
        print(f"  injected {len(secrets)} secrets into conversation")

    # Build session URL from conversation ID and inject as a secret so
    # the agent can use $AUTOMATION_SESSION_URL in bash commands.
    if not IS_LOCAL_MODE and api_url:
        session_url = f"{api_url}/canvas/conversations/{conversation.id}"
        conversation.update_secrets({"AUTOMATION_SESSION_URL": session_url})
        print(f"  session URL: {session_url}")

    try:
        print("  sending prompt")
        conversation.send_message(USER_PROMPT)
        conversation.run()
    finally:
        try:
            conversation.close()
        except Exception as e:
            print(f"  WARNING: conversation.close() failed (non-fatal): {e}")

    print("  conversation completed successfully")

    print("\n=== RESULT ===")
    print("ALL_OK")
