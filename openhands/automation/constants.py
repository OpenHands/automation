"""Protocol constants for the automation service.

This module contains ONLY values that are baked into the system design and
CANNOT be safely changed without breaking compatibility. These are NOT
tunable operational parameters.

For tunable settings (timeouts, limits, batch sizes), see config.py which
exposes them as environment variables for Helm chart configuration.

WARNING: Changing any value here requires careful analysis of:
- Database migrations (if stored in DB)
- API compatibility (if exposed to clients)
- Sandbox conventions (if expected by SDK/runtime)
"""

# ---------------------------------------------------------------------------
# Sandbox protocol conventions
# ---------------------------------------------------------------------------

# Agent's working directory inside the sandbox. This path is:
# - Expected by the OpenHands SDK
# - Used by clone_repos() for repository placement
# - Referenced in automation scripts (sdk_main.py)
# DO NOT CHANGE: Would break all existing automations and SDK integration.
WORK_DIR = "/workspace/project"

# Path where tarballs are extracted inside the sandbox. This is:
# - Written by the sandbox initialization script
# - Read by the automation entrypoint
# DO NOT CHANGE: Would break tarball extraction in running sandboxes.
TARBALL_PATH = "/tmp/automation.tar.gz"

# model profile names mirror the agent-server profile-store constraints.
MODEL_PROFILE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
