#!/bin/bash
# Install the OpenHands SDK from PyPI.
set -e

SDK_VERSION="1.19.1"
echo "[setup] installing openhands SDK ($SDK_VERSION)"
# Package order doesn't matter - pip resolves all dependencies together
pip install -q --no-cache-dir \
  "openhands-sdk==${SDK_VERSION}" \
  "openhands-tools==${SDK_VERSION}" \
  "openhands-workspace==${SDK_VERSION}"
echo "[setup] done"
