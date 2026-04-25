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


# ---------------------------------------------------------------------------
# Backward compatibility aliases (DEPRECATED)
# ---------------------------------------------------------------------------
# These previously lived here but are now configurable via config.py.
# Kept as imports for backward compatibility with code that references them.
# TODO: Remove after migrating all consumers to use config.


def __getattr__(name: str):
    """Lazy attribute access for deprecated constants."""
    # Import here to avoid circular import at module load time
    from datetime import timedelta

    from automation.config import get_config

    config = get_config()

    # Map old constant names to new config paths
    deprecated_map = {
        "MAX_RUN_DURATION": lambda: timedelta(
            seconds=config.sandbox.max_run_duration
        ),
        "MAX_RUN_DURATION_SECONDS": lambda: config.sandbox.max_run_duration,
        "SANDBOX_POLL_INTERVAL": lambda: config.sandbox.sandbox_poll_interval,
        "SANDBOX_READY_TIMEOUT": lambda: config.sandbox.sandbox_ready_timeout,
        "EXTERNAL_DOWNLOAD_TIMEOUT": lambda: config.sandbox.external_download_timeout,
        "EXTERNAL_MAX_FILESIZE": lambda: config.sandbox.external_max_filesize,
        "RATE_LIMIT_MIN_WAIT": lambda: config.sandbox.rate_limit_min_wait,
        "RATE_LIMIT_MAX_WAIT": lambda: config.sandbox.rate_limit_max_wait,
        "RATE_LIMIT_MAX_RETRIES": lambda: config.sandbox.rate_limit_max_retries,
    }

    if name in deprecated_map:
        import warnings

        msg = (
            f"constants.{name} is deprecated. "
            f"Use get_config().sandbox.{name.lower()} instead."
        )
        warnings.warn(msg, DeprecationWarning, stacklevel=2)
        return deprecated_map[name]()

    raise AttributeError(f"module 'automation.constants' has no attribute {name!r}")
