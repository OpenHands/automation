#!/bin/bash
# Install the OpenHands SDK from GitHub into an isolated virtual environment.
#
# Each automation run gets its own venv in its work directory, ensuring:
# - No conflicts between concurrent automation runs
# - Clean isolation of dependencies
# - No pollution of the system Python environment
#
# Note: Repository cloning is handled by the SDK's workspace methods inside main.py.
set -e

SDK_BRANCH="main"

echo "[setup] Creating isolated virtual environment"
uv venv .venv --quiet

echo "[setup] Installing OpenHands SDK from GitHub (branch: $SDK_BRANCH)"
uv pip install --quiet \
  "openhands-sdk @ git+https://github.com/OpenHands/software-agent-sdk.git@${SDK_BRANCH}#subdirectory=openhands-sdk" \
  "openhands-tools @ git+https://github.com/OpenHands/software-agent-sdk.git@${SDK_BRANCH}#subdirectory=openhands-tools" \
  "openhands-workspace @ git+https://github.com/OpenHands/software-agent-sdk.git@${SDK_BRANCH}#subdirectory=openhands-workspace"

echo "[setup] Done"
