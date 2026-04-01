#!/bin/bash
# Install the OpenHands SDK from PyPI (released versions).
# Version pinned to match pyproject.toml dependency.
set -e

echo "[setup] installing openhands SDK from PyPI"
pip install -q --no-cache-dir \
  openhands-sdk==1.16.0 \
  openhands-workspace \
  openhands-tools
echo "[setup] done"
