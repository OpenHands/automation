#!/bin/bash
# Install the OpenHands SDK from PR branch for testing.
#
# TESTING: SDK PR #3054 - MCP config fix
# https://github.com/OpenHands/software-agent-sdk/pull/3054
#
# This installs from the fix/mcp-config-format-passthrough branch
# to test the MCP config changes before PyPI release.
#
# Note: Repository cloning is handled by the SDK's
# OpenHandsCloudWorkspace.clone_repos() method inside main.py.
set -e

SDK_BRANCH="fix/mcp-config-format-passthrough"
SDK_REPO="https://github.com/OpenHands/software-agent-sdk.git"

echo "[setup] Installing OpenHands SDK from PR branch: $SDK_BRANCH"
echo "[setup] This is a TEST build for SDK PR #3054"

# Install all three packages from the PR branch
pip install -q --no-cache-dir \
  "openhands-sdk @ git+${SDK_REPO}@${SDK_BRANCH}#subdirectory=openhands-sdk" \
  "openhands-tools @ git+${SDK_REPO}@${SDK_BRANCH}#subdirectory=openhands-tools" \
  "openhands-workspace @ git+${SDK_REPO}@${SDK_BRANCH}#subdirectory=openhands-workspace"

echo "[setup] Done - SDK installed from branch: $SDK_BRANCH"
