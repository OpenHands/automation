#!/bin/bash
# Install the OpenHands SDK from branch.
#
# Note: Repository cloning is handled by the SDK's
# OpenHandsCloudWorkspace.clone_repos() method inside main.py.
set -e

SDK_REF="openhands/add-conversation-id-to-callback"

echo "[setup] Installing OpenHands SDK (branch: $SDK_REF)"
pip install -q --no-cache-dir \
  "openhands-workspace @ git+https://github.com/OpenHands/software-agent-sdk.git@${SDK_REF}#subdirectory=openhands-workspace" \
  "openhands-sdk @ git+https://github.com/OpenHands/software-agent-sdk.git@${SDK_REF}#subdirectory=openhands-sdk" \
  "openhands-tools @ git+https://github.com/OpenHands/software-agent-sdk.git@${SDK_REF}#subdirectory=openhands-tools"

echo "[setup] Done"
