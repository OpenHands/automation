#!/bin/bash
# Install the OpenHands SDK from PyPI into an isolated virtual environment.
set -e

if [ -z "${AUTOMATION_API_URL:-}" ]; then
    echo "[setup] ERROR: AUTOMATION_API_URL is required to fetch the SDK version" >&2
    exit 1
fi

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
SDK_VERSION=$(curl -sf "${AUTOMATION_API_URL}/sdk-version" \
  | ${PYTHON_JSON} -c "import sys, json; print(json.load(sys.stdin)['version'])" 2>/dev/null)
set -e
if [ -z "$SDK_VERSION" ]; then
    echo "[setup] ERROR: Failed to fetch SDK version from ${AUTOMATION_API_URL}/sdk-version" >&2
    exit 1
fi

echo "[setup] Creating isolated virtual environment"
uv venv .venv --python '>=3.12' --quiet

echo "[setup] Installing OpenHands SDK from PyPI (version: $SDK_VERSION)"
uv pip install --quiet \
  "openhands-sdk==${SDK_VERSION}" \
  "openhands-tools==${SDK_VERSION}" \
  "openhands-workspace==${SDK_VERSION}"

echo "[setup] Done"
