#!/bin/bash
# Install the OpenHands SDK.
#
# In local mode, installs from GitHub branch with settings persistence support.
# In cloud mode, installs from PyPI.
#
# Note: Repository cloning is handled by the SDK's
# OpenHandsCloudWorkspace.clone_repos() method inside main.py.
set -e

SDK_VERSION="1.18.1"
SDK_BRANCH_SHA="6ec01438006a81c57ef313e016b28179363c5e62"

# Detect local mode by AGENT_SERVER_URL presence
if [ -n "$AGENT_SERVER_URL" ]; then
  echo "[setup] Local mode detected - installing SDK from branch ($SDK_BRANCH_SHA)"
  pip install -q --no-cache-dir \
    "openhands-sdk @ git+https://github.com/OpenHands/software-agent-sdk.git@${SDK_BRANCH_SHA}#subdirectory=openhands-sdk" \
    "openhands-workspace @ git+https://github.com/OpenHands/software-agent-sdk.git@${SDK_BRANCH_SHA}#subdirectory=openhands-workspace" \
    "openhands-tools @ git+https://github.com/OpenHands/software-agent-sdk.git@${SDK_BRANCH_SHA}#subdirectory=openhands-tools"
else
  echo "[setup] Cloud mode - installing OpenHands SDK from PyPI (version: $SDK_VERSION)"
  pip install -q --no-cache-dir \
    "openhands-sdk==${SDK_VERSION}" \
    "openhands-workspace==${SDK_VERSION}" \
    "openhands-tools==${SDK_VERSION}"
fi

echo "[setup] Done"
