#!/bin/bash
# Install the OpenHands SDK from PyPI.
#
# Note: Repository cloning is handled by the SDK's
# OpenHandsCloudWorkspace.clone_repos() method inside main.py.
set -e

SDK_VERSION="1.19.1"

echo "[setup] Installing OpenHands SDK (version: $SDK_VERSION)"
# Package order doesn't matter - pip resolves all dependencies together
pip install -q --no-cache-dir \
  "openhands-sdk==${SDK_VERSION}" \
  "openhands-tools==${SDK_VERSION}" \
  "openhands-workspace==${SDK_VERSION}"

echo "[setup] Done"
