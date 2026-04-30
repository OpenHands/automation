#!/bin/bash
# Entrypoint script for self-hosted automation container
#
# Sets sensible defaults for local mode while allowing runtime overrides.
# This avoids baking sensitive-looking values into Docker image layers.

set -e

# ---- Local mode defaults ----
# These are safe defaults for self-hosted deployments where the agent server
# is embedded in the same container. Override with real secrets for production.

# API key for automation service to communicate with embedded agent server
export AUTOMATION_AGENT_SERVER_API_KEY="${AUTOMATION_AGENT_SERVER_API_KEY:-local-embedded}"

# Session API key for agent server authentication
export SESSION_API_KEY="${SESSION_API_KEY:-local-embedded}"

# Auth bypass for self-hosted mode (no OpenHands Cloud auth required)
# Set to "false" to require authentication
export AUTOMATION_AUTH_DISABLED="${AUTOMATION_AUTH_DISABLED:-true}"

# ---- Validation ----
# Warn if using defaults in what looks like a production setup
if [[ "${AUTOMATION_AGENT_SERVER_API_KEY}" == "local-embedded" ]] && \
   [[ -n "${PRODUCTION}" || -n "${OPENHANDS_API_KEY}" ]]; then
    echo "WARNING: Using default API keys in what appears to be a production setup."
    echo "         Consider setting AUTOMATION_AGENT_SERVER_API_KEY and SESSION_API_KEY."
fi

# ---- Run migrations if database doesn't exist ----
if [[ "${AUTOMATION_DB_URL}" == sqlite* ]] && [[ ! -f /data/automations.db ]]; then
    echo "Initializing SQLite database..."
    cd /app && alembic upgrade head
fi

# ---- Execute the main command ----
exec "$@"
