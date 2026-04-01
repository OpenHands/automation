#!/bin/bash
# Install the OpenHands SDK from PyPI (released versions).
set -e

echo "[setup] installing openhands SDK from PyPI"
pip install -q --no-cache-dir \
  openhands-sdk \
  openhands-workspace \
  openhands-tools
echo "[setup] done"
