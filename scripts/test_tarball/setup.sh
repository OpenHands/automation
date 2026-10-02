#!/bin/bash
# Install the OpenHands SDK from PyPI into an isolated virtual environment.
set -e

if [ -z "${AUTOMATION_API_URL:-}" ]; then
    echo "[setup] ERROR: AUTOMATION_API_URL is required to fetch the SDK version" >&2
    exit 1
fi

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

echo "[setup] Creating isolated virtual environment"
uv venv .venv --python '>=3.12' --quiet

echo "[setup] Installing OpenHands SDK packages (version: $SDK_VERSION)"
uv pip install --quiet \
  "$SDK_SPEC" \
  "$TOOLS_SPEC" \
  "$WORKSPACE_SPEC"

echo "[setup] Done"
