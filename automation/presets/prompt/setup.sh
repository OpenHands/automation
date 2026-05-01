#!/bin/bash
# Install the OpenHands SDK from git branch.
set -e

SDK_BRANCH="feat/settings-persistence"

echo "[setup] Installing OpenHands SDK from branch: $SDK_BRANCH"
pip install -q --no-cache-dir \
  "openhands-sdk @ git+https://github.com/OpenHands/software-agent-sdk.git@${SDK_BRANCH}#subdirectory=openhands-sdk" \
  "openhands-workspace @ git+https://github.com/OpenHands/software-agent-sdk.git@${SDK_BRANCH}#subdirectory=openhands-workspace" \
  "openhands-tools @ git+https://github.com/OpenHands/software-agent-sdk.git@${SDK_BRANCH}#subdirectory=openhands-tools"

echo "[setup] Done"
