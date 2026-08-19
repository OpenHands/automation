#!/bin/bash
# Install OpenHands SDK packages into an isolated virtual environment.
#
# Each automation run gets its own venv in its work directory, ensuring:
# - No conflicts between concurrent automation runs
# - Clean isolation of dependencies
# - No pollution of the system Python environment
#
# Note: Repository cloning is handled by the SDK's workspace methods inside main.py.
#
# The SDK install specs are fetched from the automation service API on every run
# so deploying a new service version is the only step required to roll out SDK
# package updates — no tarball re-generation needed.
set -e

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
# Pin >=3.12 so uv doesn't default to an older system Python (e.g. macOS
# CommandLineTools 3.9), which can't satisfy openhands-sdk's requires-python.
uv venv .venv --python '>=3.12' --quiet

echo "[setup] Installing OpenHands SDK packages"
uv pip install --quiet -r "$SDK_REQUIREMENTS"

echo "[setup] Done"
