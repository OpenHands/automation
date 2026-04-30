#!/bin/bash
# Entrypoint script for combined Agent Server GUI + Automation container
#
# Sets sensible defaults for local mode while allowing runtime overrides.

set -e

# ---- Local mode defaults ----

# Auth bypass for self-hosted mode (no OpenHands Cloud auth required)
export AUTOMATION_AUTH_DISABLED="${AUTOMATION_AUTH_DISABLED:-true}"

# ---- LLM Configuration ----
# Support simplified env var names (LLM_MODEL, LLM_API_KEY, LLM_BASE_URL)
# and propagate to automation service

if [[ -n "${LLM_MODEL}" ]]; then
    export AUTOMATION_LLM_MODEL="${LLM_MODEL}"
fi

if [[ -n "${LLM_API_KEY}" ]]; then
    export AUTOMATION_LLM_API_KEY="${LLM_API_KEY}"
fi

if [[ -n "${LLM_BASE_URL}" ]]; then
    export AUTOMATION_LLM_BASE_URL="${LLM_BASE_URL}"
fi

# ---- Create data directories ----
# /root/.openhands - agent-server conversations (mount your ~/.openhands)
# /data/storage - automation tarball uploads
mkdir -p /root/.openhands/conversations /data/storage /tmp/openhands-sandboxes
chmod 755 /root/.openhands /root/.openhands/conversations /data/storage /tmp/openhands-sandboxes

# ---- Run database migrations ----

# Automation service migrations (using automation venv)
if [[ "${AUTOMATION_DB_URL}" == sqlite* ]] && [[ ! -f /data/automations.db ]]; then
    echo "Initializing Automation SQLite database..."
    cd /app/automation-src && /app/automation-venv/bin/alembic upgrade head
fi

# ---- Startup message ----
echo "Starting OpenHands Local (Agent Server GUI + Automations)..."
echo "  - GUI:         http://localhost:8000/"
echo "  - Automations: http://localhost:8000/automations/"
echo "  - API Docs:    http://localhost:8000/api/automation/docs"
echo ""
echo "Note: Configure LLM settings in the GUI at Settings > LLM"
echo "      or set LLM_MODEL and LLM_API_KEY environment variables."

# ---- Execute the main command ----
exec "$@"
