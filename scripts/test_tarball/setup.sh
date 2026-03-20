#!/bin/bash
# Install the OpenHands SDK packages from main.
# Once SDK PR #2490 (saas_runtime_mode) is merged, switch to a release version.
set -e

echo "[setup] installing openhands SDK (main branch)"
pip install -q \
  "openhands-workspace @ git+https://github.com/OpenHands/software-agent-sdk.git@main#subdirectory=openhands-workspace" \
  "openhands-sdk @ git+https://github.com/OpenHands/software-agent-sdk.git@main#subdirectory=openhands-sdk" \
  "openhands-tools @ git+https://github.com/OpenHands/software-agent-sdk.git@main#subdirectory=openhands-tools"
echo "[setup] done"
