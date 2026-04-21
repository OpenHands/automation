#!/bin/bash
# Install the OpenHands SDK from PyPI and clone any configured repositories.
# All versions pinned to avoid potential issues due to version mismatch.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "[setup] Installing OpenHands SDK from PyPI"
pip install -q --no-cache-dir \
  openhands-sdk==1.16.1 \
  openhands-workspace==1.16.1 \
  openhands-tools==1.16.1

# Clone repos if config and clone script exist
# Note: Repo clone failures are non-fatal - warnings printed but script exits 0
if [ -f "$SCRIPT_DIR/repos_config.json" ] && [ -f "$SCRIPT_DIR/clone_repos.py" ]; then
    echo "[setup] Found repos_config.json, cloning repositories..."
    python3 "$SCRIPT_DIR/clone_repos.py"
fi

echo "[setup] Done"
