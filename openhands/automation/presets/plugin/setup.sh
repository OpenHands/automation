#!/bin/bash
# Install the OpenHands SDK from PyPI into an isolated virtual environment.
#
# Each automation run gets its own venv in its work directory, ensuring:
# - No conflicts between concurrent automation runs
# - Clean isolation of dependencies
# - No pollution of the system Python environment
#
# Note: Repository cloning is handled by the SDK's workspace methods inside main.py.
#
# OPENHANDS_SDK_VERSION is injected by the automation dispatcher and matches
# the SDK version the service is currently running, so the sandbox always
# installs a compatible SDK without any hardcoded version pins here.
set -e

SDK_VERSION="${OPENHANDS_SDK_VERSION}"

echo "[setup] Creating isolated virtual environment"
uv venv .venv --quiet

echo "[setup] Installing OpenHands SDK from PyPI (version: $SDK_VERSION)"
uv pip install --quiet \
  "openhands-sdk==${SDK_VERSION}" \
  "openhands-tools==${SDK_VERSION}" \
  "openhands-workspace==${SDK_VERSION}"

echo "[setup] Done"
