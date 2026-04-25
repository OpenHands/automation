#!/bin/bash
# Install the OpenHands SDK from main branch.
set -e

SDK_REF="main"
echo "[setup] installing openhands SDK ($SDK_REF)"
pip install -q --no-cache-dir \
  "openhands-workspace @ git+https://github.com/OpenHands/software-agent-sdk.git@${SDK_REF}#subdirectory=openhands-workspace" \
  "openhands-sdk @ git+https://github.com/OpenHands/software-agent-sdk.git@${SDK_REF}#subdirectory=openhands-sdk" \
  "openhands-tools @ git+https://github.com/OpenHands/software-agent-sdk.git@${SDK_REF}#subdirectory=openhands-tools"
echo "[setup] done"
