#!/bin/bash
# Install the OpenHands SDK from the saas-runtime-mode feature branch.
# Once merged, switch to a released version or @main.
set -e

SDK_REF="feat/saas-runtime-mode"
echo "[setup] installing openhands SDK ($SDK_REF)"
pip install -q --no-cache-dir \
  "openhands-workspace @ git+https://github.com/OpenHands/software-agent-sdk.git@${SDK_REF}#subdirectory=openhands-workspace" \
  "openhands-sdk @ git+https://github.com/OpenHands/software-agent-sdk.git@${SDK_REF}#subdirectory=openhands-sdk" \
  "openhands-tools @ git+https://github.com/OpenHands/software-agent-sdk.git@${SDK_REF}#subdirectory=openhands-tools"
echo "[setup] done"
