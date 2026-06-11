"""Cross-platform preset bootstrap for plugin automations.

This script runs before sdk_main.py and uses only the Python standard library.
It creates the per-run virtual environment, installs the matching OpenHands SDK
packages, then re-execs the generated main.py with the venv interpreter.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.request import Request, urlopen

SDK_PACKAGES = (
    "openhands-sdk",
    "openhands-tools",
    "openhands-workspace",
)
SCRIPT_DIR = Path(__file__).resolve().parent
VENV_DIR = SCRIPT_DIR / ".venv"
MAIN_PATH = SCRIPT_DIR / "main.py"
PYTHON_REQUIREMENT = ">=3.12"
SDK_VERSION_ENV_VAR = "OPENHANDS_SDK_VERSION"
AUTOMATION_API_URL_ENV_VAR = "AUTOMATION_API_URL"
SDK_VERSION_PATH = "/sdk-version"


def _require_uv() -> str:
    uv_path = shutil.which("uv")
    if not uv_path:
        raise SystemExit("[bootstrap] ERROR: uv is required but was not found in PATH")
    return uv_path


def _fetch_sdk_version() -> str:
    if version := os.environ.get(SDK_VERSION_ENV_VAR):
        return version

    api_url = os.environ.get(AUTOMATION_API_URL_ENV_VAR, "").rstrip("/")
    if not api_url:
        raise SystemExit(
            f"[bootstrap] ERROR: {AUTOMATION_API_URL_ENV_VAR} is required to fetch the SDK version"
        )

    request = Request(
        f"{api_url}{SDK_VERSION_PATH}",
        headers={"Accept": "application/json"},
    )
    with urlopen(request) as response:
        payload = json.load(response)

    version = payload.get("version")
    if not version:
        raise SystemExit("[bootstrap] ERROR: sdk-version response did not include a version")
    return version


def _run_checked(*args: str) -> None:
    print("[bootstrap] running:", " ".join(args))
    subprocess.run(args, check=True)


def _venv_python_path() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def main() -> None:
    uv_path = _require_uv()
    sdk_version = _fetch_sdk_version()
    print(f"[bootstrap] Creating virtual environment in {VENV_DIR}")
    _run_checked(uv_path, "venv", str(VENV_DIR), "--python", PYTHON_REQUIREMENT, "--quiet")

    print(f"[bootstrap] Installing OpenHands SDK packages at version {sdk_version}")
    install_args = [uv_path, "pip", "install", "--quiet"]
    install_args.extend(f"{package}=={sdk_version}" for package in SDK_PACKAGES)
    _run_checked(*install_args)

    venv_python = _venv_python_path()
    if not venv_python.exists():
        raise SystemExit(
            f"[bootstrap] ERROR: Expected virtualenv Python at {venv_python}, but it was not created"
        )
    if not MAIN_PATH.exists():
        raise SystemExit(f"[bootstrap] ERROR: Expected generated main.py at {MAIN_PATH}")

    print(f"[bootstrap] Launching generated automation with {venv_python}")
    os.execv(str(venv_python), [str(venv_python), str(MAIN_PATH)])


if __name__ == "__main__":
    main()
