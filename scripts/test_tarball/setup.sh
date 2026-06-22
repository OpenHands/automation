#!/bin/bash
# Install the OpenHands SDK from PyPI into an isolated virtual environment.
set -e

SDK_VERSION="1.22.0"

echo "[setup] Creating isolated virtual environment"
uv venv .venv --quiet

echo "[setup] Installing OpenHands SDK from PyPI (version: $SDK_VERSION)"
uv pip install --quiet \
  "openhands-sdk==${SDK_VERSION}" \
  "openhands-tools==${SDK_VERSION}" \
  "openhands-workspace==${SDK_VERSION}"

echo "[setup] Done"
