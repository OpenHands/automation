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

echo "[setup] Fetching SDK version from automation service"
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
SDK_METADATA=$(curl -sf "${AUTOMATION_API_URL}/sdk-version")
SDK_VERSION=$(printf '%s' "$SDK_METADATA" \
  | ${PYTHON_JSON} -c "import sys, json; print(json.load(sys.stdin)['version'])" 2>/dev/null)
SDK_INSTALL_SPEC=$(printf '%s' "$SDK_METADATA" \
  | ${PYTHON_JSON} -c "import sys, json; data=json.load(sys.stdin); print(data.get('install_spec') or ('openhands-sdk==' + data['version']))" 2>/dev/null)
TOOLS_INSTALL_SPEC=$(printf '%s' "$SDK_METADATA" \
  | ${PYTHON_JSON} -c "import sys, json; data=json.load(sys.stdin); print(data.get('tools_install_spec') or ('openhands-tools==' + data['version']))" 2>/dev/null)
set -e
if [ -z "$SDK_VERSION" ] || [ -z "$SDK_INSTALL_SPEC" ] || [ -z "$TOOLS_INSTALL_SPEC" ]; then
    echo "[setup] ERROR: Failed to fetch SDK install metadata from ${AUTOMATION_API_URL}/sdk-version" >&2
    exit 1
fi

echo "[setup] Creating isolated virtual environment"
# Pin >=3.12 so uv doesn't default to an older system Python (e.g. macOS
# CommandLineTools 3.9), which can't satisfy openhands-sdk's requires-python.
uv venv .venv --python '>=3.12' --quiet

echo "[setup] Installing OpenHands SDK ($SDK_INSTALL_SPEC)"
uv pip install --quiet \
  "$SDK_INSTALL_SPEC" \
  "$TOOLS_INSTALL_SPEC" \
  "openhands-workspace==${SDK_VERSION}"

echo "[setup] Done"
