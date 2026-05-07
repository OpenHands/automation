#!/bin/bash
# Install the OpenHands SDK from GitHub.
#
# Note: Repository cloning is handled by the SDK's workspace methods inside main.py.
set -e

SDK_BRANCH="openhands/add-remote-workspace-methods-3094"

echo "[setup] Installing OpenHands SDK from GitHub (branch: $SDK_BRANCH)"
pip install -q --no-cache-dir \
  "openhands-sdk @ git+https://github.com/OpenHands/software-agent-sdk.git@${SDK_BRANCH}#subdirectory=openhands-sdk" \
  "openhands-tools @ git+https://github.com/OpenHands/software-agent-sdk.git@${SDK_BRANCH}#subdirectory=openhands-tools"

echo "[setup] Done"
