#!/bin/bash
# Install the OpenHands SDK from release tag.
#
# Note: Repository cloning is handled by the SDK's
# OpenHandsCloudWorkspace.clone_repos() method inside main.py.
set -e

SDK_REF="v1.19.1"

echo "[setup] Installing OpenHands SDK (version: $SDK_REF)"
pip install -q --no-cache-dir \
  "openhands-workspace @ git+https://github.com/OpenHands/software-agent-sdk.git@${SDK_REF}#subdirectory=openhands-workspace" \
  "openhands-sdk @ git+https://github.com/OpenHands/software-agent-sdk.git@${SDK_REF}#subdirectory=openhands-sdk" \
  "openhands-tools @ git+https://github.com/OpenHands/software-agent-sdk.git@${SDK_REF}#subdirectory=openhands-tools"

echo "[setup] Done"
