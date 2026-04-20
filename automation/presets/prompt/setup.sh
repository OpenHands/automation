#!/bin/bash
# Install the OpenHands SDK from PyPI and clone any configured repositories.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "[setup] Installing OpenHands SDK from PyPI"
pip install -q --no-cache-dir \
  openhands-sdk \
  openhands-workspace \
  openhands-tools

# Clone repos if config and clone script exist
if [ -f "$SCRIPT_DIR/repos_config.json" ] && [ -f "$SCRIPT_DIR/clone_repos.py" ]; then
    echo "[setup] Found repos_config.json, cloning repositories..."
    python3 "$SCRIPT_DIR/clone_repos.py"
fi

echo "[setup] Done"
