#!/bin/bash
# Install the OpenHands SDK from PyPI.
# All versions pinned to avoid potential issues due to version mismatch.
#
# Note: Repository cloning is now handled by the SDK's
# OpenHandsCloudWorkspace.clone_repos() method inside main.py.
set -e

echo "[setup] Installing OpenHands SDK from PyPI"
pip install -q --no-cache-dir \
  openhands-sdk==1.17.0 \
  openhands-workspace==1.17.0 \
  openhands-tools==1.17.0

echo "[setup] Done"
