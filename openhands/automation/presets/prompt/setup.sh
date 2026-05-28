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
set +e
SDK_VERSION=$(curl -sf "${AUTOMATION_API_URL}/sdk-version" \
  | python3 -c "import sys, json; print(json.load(sys.stdin)['version'])" 2>/dev/null)
set -e
if [ -z "$SDK_VERSION" ]; then
    echo "[setup] ERROR: Failed to fetch SDK version from ${AUTOMATION_API_URL}/sdk-version" >&2
    exit 1
fi

echo "[setup] Creating isolated virtual environment"
# Pin >=3.12 so uv doesn't default to an older system Python (e.g. macOS
# CommandLineTools 3.9), which can't satisfy openhands-sdk's requires-python.
uv venv .venv --python '>=3.12' --quiet

echo "[setup] Installing OpenHands SDK from PyPI (version: $SDK_VERSION)"
uv pip install --quiet \
  "openhands-sdk==${SDK_VERSION}" \
  "openhands-tools==${SDK_VERSION}" \
  "openhands-workspace==${SDK_VERSION}"

echo "[setup] Done"
