#!/bin/bash
# Install the OpenHands SDK from git into an isolated virtual environment.
#
# Each automation run gets its own venv in its work directory, ensuring:
# - No conflicts between concurrent automation runs
# - Clean isolation of dependencies
# - No pollution of the system Python environment
#
# Note: Repository cloning is handled by the SDK's workspace methods inside main.py.
set -e

SDK_GIT_REF="feat/api-remote-workspace-callback"
SDK_REPO="https://github.com/OpenHands/software-agent-sdk.git"

echo "[setup] Creating isolated virtual environment"
uv venv .venv --quiet

echo "[setup] Installing OpenHands SDK from git (ref: $SDK_GIT_REF)"
uv pip install --quiet \
  "openhands-sdk @ git+${SDK_REPO}@${SDK_GIT_REF}#subdirectory=openhands-sdk" \
  "openhands-tools @ git+${SDK_REPO}@${SDK_GIT_REF}#subdirectory=openhands-tools" \
  "openhands-workspace @ git+${SDK_REPO}@${SDK_GIT_REF}#subdirectory=openhands-workspace"

echo "[setup] Done"
