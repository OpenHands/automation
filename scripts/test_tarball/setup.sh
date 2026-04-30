#!/bin/bash
# Install the OpenHands SDK from PyPI.
set -e

SDK_VERSION="1.19.1"
echo "[setup] installing openhands SDK ($SDK_VERSION)"
pip install -q --no-cache-dir \
  "openhands-workspace>=${SDK_VERSION}" \
  "openhands-sdk>=${SDK_VERSION}" \
  "openhands-tools>=${SDK_VERSION}"
echo "[setup] done"
