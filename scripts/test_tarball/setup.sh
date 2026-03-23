#!/bin/bash
# Install the OpenHands SDK packages from PR #2490 (saas_runtime_mode).
# Once merged, switch to a release version or @main.
set -e

SDK_REF="feat/saas-runtime-mode"
echo "[setup] installing openhands SDK ($SDK_REF)"
pip install -q --no-cache-dir \
  "openhands-workspace @ git+https://github.com/OpenHands/software-agent-sdk.git@${SDK_REF}#subdirectory=openhands-workspace" \
  "openhands-sdk @ git+https://github.com/OpenHands/software-agent-sdk.git@${SDK_REF}#subdirectory=openhands-sdk" \
  "openhands-tools @ git+https://github.com/OpenHands/software-agent-sdk.git@${SDK_REF}#subdirectory=openhands-tools"
echo "[setup] done"
