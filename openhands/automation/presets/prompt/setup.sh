#!/bin/bash
# Install the OpenHands SDK from PyPI into an isolated virtual environment.
#
# Each automation run gets its own venv in its work directory, ensuring:
# - No conflicts between concurrent automation runs
# - Clean isolation of dependencies
# - No pollution of the system Python environment
#
# Note: Repository cloning is handled by the SDK's workspace methods inside main.py.
#
# The SDK version is fetched from the automation service API on every run so
# that deploying a new service version is the only step required to roll out a
# new SDK — no tarball re-generation or hardcoded version pins needed.
set -e

echo "[setup] Fetching SDK install info from automation service"
PYTHON_JSON=python3
if ! command -v python3 >/dev/null 2>&1; then
    if command -v python >/dev/null 2>&1; then
        PYTHON_JSON=python
    elif command -v py >/dev/null 2>&1; then
        PYTHON_JSON='py -3'
    else
        echo "[setup] ERROR: python3, python, or py is required to parse SDK version" >&2
        exit 1
    fi
fi
set +e
SDK_INFO=$(curl -sf "${AUTOMATION_API_URL}/sdk-version")
SDK_VERSION=$(printf '%s' "$SDK_INFO" \
  | ${PYTHON_JSON} -c "import sys, json; print(json.load(sys.stdin)['version'])" 2>/dev/null)
SDK_SPEC=$(printf '%s' "$SDK_INFO" \
  | ${PYTHON_JSON} -c "import sys, json; d=json.load(sys.stdin); print(d.get('packages', {}).get('openhands-sdk') or 'openhands-sdk==' + d['version'])" 2>/dev/null)
TOOLS_SPEC=$(printf '%s' "$SDK_INFO" \
  | ${PYTHON_JSON} -c "import sys, json; d=json.load(sys.stdin); print(d.get('packages', {}).get('openhands-tools') or 'openhands-tools==' + d['version'])" 2>/dev/null)
WORKSPACE_SPEC=$(printf '%s' "$SDK_INFO" \
  | ${PYTHON_JSON} -c "import sys, json; d=json.load(sys.stdin); print(d.get('packages', {}).get('openhands-workspace') or 'openhands-workspace==' + d['version'])" 2>/dev/null)
set -e
if [ -z "$SDK_VERSION" ] || [ -z "$SDK_SPEC" ] || [ -z "$TOOLS_SPEC" ] || [ -z "$WORKSPACE_SPEC" ]; then
    echo "[setup] ERROR: Failed to fetch SDK install info from ${AUTOMATION_API_URL}/sdk-version" >&2
    exit 1
fi

# Best-effort progress phase for the dashboard — must never fail the setup.
PHASE_TOKEN="${AUTOMATION_CALLBACK_API_KEY:-${OPENHANDS_API_KEY:-}}"
if [ -n "${AUTOMATION_PHASE_URL:-}" ] && [ -n "$PHASE_TOKEN" ]; then
    curl -sf -m 5 -X POST "$AUTOMATION_PHASE_URL" \
      -H "Authorization: Bearer $PHASE_TOKEN" \
      -H "Content-Type: application/json" \
      -d '{"phase": "Installing dependencies"}' >/dev/null 2>&1 || true
fi

echo "[setup] Creating isolated virtual environment"
# <3.14: otherwise uv may pick free-threaded 3.14t, where the SDK fails to install.
uv venv .venv --python 'cpython>=3.12,<3.14' --quiet

VENV_PYTHON=.venv/bin/python
if [ ! -x "$VENV_PYTHON" ]; then
    VENV_PYTHON=.venv/Scripts/python.exe
fi
if [ ! -x "$VENV_PYTHON" ]; then
    echo "[setup] ERROR: Python executable is missing from .venv" >&2
    exit 1
fi

echo "[setup] Installing OpenHands SDK packages (version: $SDK_VERSION)"
uv pip install --python "$VENV_PYTHON" --quiet \
  "$SDK_SPEC" \
  "$TOOLS_SPEC" \
  "$WORKSPACE_SPEC"

if ! "$VENV_PYTHON" -c 'import openhands.sdk, openhands.tools, openhands.workspace'; then
    echo "[setup] ERROR: OpenHands SDK is not importable from .venv" >&2
    exit 1
fi

echo "[setup] Done"
