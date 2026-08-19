#!/bin/bash
# Install OpenHands SDK packages into an isolated virtual environment.
set -e

if [ -z "${AUTOMATION_API_URL:-}" ]; then
    echo "[setup] ERROR: AUTOMATION_API_URL is required to fetch the SDK version" >&2
    exit 1
fi

echo "[setup] Fetching SDK install specs from automation service"
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
SDK_REQUIREMENTS=.openhands-sdk-requirements.txt
set +e
curl -sf "${AUTOMATION_API_URL}/sdk-version" \
  | ${PYTHON_JSON} -c "import sys, json; data=json.load(sys.stdin); specs=data.get('install_specs') or [f'openhands-sdk=={data[\"version\"]}', f'openhands-tools=={data[\"version\"]}', f'openhands-workspace=={data[\"version\"]}']; print('\\n'.join(specs))" \
  > "$SDK_REQUIREMENTS"
SETUP_STATUS=$?
set -e
if [ $SETUP_STATUS -ne 0 ] || [ ! -s "$SDK_REQUIREMENTS" ]; then
    echo "[setup] ERROR: Failed to fetch SDK install specs from ${AUTOMATION_API_URL}/sdk-version" >&2
    exit 1
fi

echo "[setup] Creating isolated virtual environment"
uv venv .venv --python '>=3.12' --quiet

echo "[setup] Installing OpenHands SDK packages"
uv pip install --quiet -r "$SDK_REQUIREMENTS"

echo "[setup] Done"
