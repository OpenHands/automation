#!/bin/bash
# Install the OpenHands SDK from git.
# Uses specific commit from the SDK PR for repo cloning support.
#
# Note: Repository cloning is handled by the SDK's
# OpenHandsCloudWorkspace.clone_repos() method inside main.py.
set -e

SDK_REPO="https://github.com/OpenHands/software-agent-sdk.git"
SDK_REF="feat/add-repo-cloning-and-skill-loading"

echo "[setup] Installing OpenHands SDK from git (ref: $SDK_REF)"
pip install -q --no-cache-dir \
  "openhands-sdk @ git+${SDK_REPO}@${SDK_REF}#subdirectory=openhands-sdk" \
  "openhands-workspace @ git+${SDK_REPO}@${SDK_REF}#subdirectory=openhands-workspace" \
  "openhands-tools @ git+${SDK_REPO}@${SDK_REF}#subdirectory=openhands-tools"

echo "[setup] Done"
