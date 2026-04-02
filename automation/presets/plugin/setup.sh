#!/bin/bash
# Install the OpenHands SDK from PyPI (released versions).
# All versions pinned to avoid potential issues due to version mismatch.
set -e

echo "[setup] installing openhands SDK from PyPI"
pip install -q --no-cache-dir \
  openhands-sdk==1.16.1 \
  openhands-workspace==1.16.1 \
  openhands-tools==1.16.1
echo "[setup] done"
