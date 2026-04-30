#!/bin/bash
# Entrypoint script for combined OpenHands + Automation container
#
# Sets sensible defaults for local mode while allowing runtime overrides.

set -e

# ---- Local mode defaults ----

# API key for automation service to communicate with embedded agent server
export AUTOMATION_AGENT_SERVER_API_KEY="${AUTOMATION_AGENT_SERVER_API_KEY:-local-embedded}"

# Session API key for agent server authentication
# Note: This is set via supervisord environment for agent-server ONLY.
# Do NOT export here - it would require X-Session-API-Key header for all
# browser requests to OpenHands GUI, breaking the frontend.
# The value is passed to supervisord as AGENT_SERVER_SESSION_API_KEY
export AGENT_SERVER_SESSION_API_KEY="${SESSION_API_KEY:-local-embedded}"

# Auth bypass for self-hosted mode (no OpenHands Cloud auth required)
export AUTOMATION_AUTH_DISABLED="${AUTOMATION_AUTH_DISABLED:-true}"

# ---- LLM Configuration ----
# Support simplified env var names (LLM_MODEL, LLM_API_KEY, LLM_BASE_URL)
# and propagate to both OpenHands and Automation services

if [[ -n "${LLM_MODEL}" ]]; then
    # For OpenHands App Server settings API
    export OPENHANDS_LLM_MODEL="${LLM_MODEL}"
    # For Automation Service preset scripts
    export AUTOMATION_LLM_MODEL="${LLM_MODEL}"
fi

if [[ -n "${LLM_API_KEY}" ]]; then
    export OPENHANDS_LLM_API_KEY="${LLM_API_KEY}"
    export AUTOMATION_LLM_API_KEY="${LLM_API_KEY}"
fi

if [[ -n "${LLM_BASE_URL}" ]]; then
    export OPENHANDS_LLM_BASE_URL="${LLM_BASE_URL}"
    export AUTOMATION_LLM_BASE_URL="${LLM_BASE_URL}"
fi

# ---- Create data directories ----
# /root/.openhands - shared between OpenHands GUI and automation agent-server
# /data/storage - automation tarball uploads
mkdir -p /root/.openhands/conversations /data/storage /tmp/openhands-sandboxes
chmod 755 /root/.openhands /root/.openhands/conversations /data/storage /tmp/openhands-sandboxes

# ---- Run database migrations ----

# Automation service migrations (using automation venv)
if [[ "${AUTOMATION_DB_URL}" == sqlite* ]] && [[ ! -f /data/automations.db ]]; then
    echo "Initializing Automation SQLite database..."
    cd /app/automation-src && /app/automation-venv/bin/alembic upgrade head
fi

# OpenHands migrations are handled by the app's lifespan startup

# ---- Validation warnings ----
if [[ "${AUTOMATION_AGENT_SERVER_API_KEY}" == "local-embedded" ]] && \
   [[ -n "${PRODUCTION}" || -n "${OPENHANDS_API_KEY}" ]]; then
    echo "WARNING: Using default API keys in what appears to be a production setup."
    echo "         Consider setting AUTOMATION_AGENT_SERVER_API_KEY and SESSION_API_KEY."
fi

if [[ -z "${LLM_MODEL}" ]] && [[ -z "${OPENHANDS_LLM_MODEL}" ]]; then
    echo "WARNING: No LLM model configured. Set LLM_MODEL environment variable."
    echo "         Example: -e LLM_MODEL=anthropic/claude-sonnet-4-20250514"
fi

if [[ -z "${LLM_API_KEY}" ]] && [[ -z "${OPENHANDS_LLM_API_KEY}" ]]; then
    echo "WARNING: No LLM API key configured. Set LLM_API_KEY environment variable."
fi

echo "Starting OpenHands Local (GUI + Automations)..."
echo "  - GUI:         http://localhost:8000/"
echo "  - Automations: http://localhost:8000/automations/"
echo "  - API Docs:    http://localhost:8000/api/automation/docs"

# ---- Execute the main command ----
exec "$@"
