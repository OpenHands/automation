"""Install the OpenHands SDK into an isolated virtual environment.

Python counterpart of the prompt and plugin presets' ``setup.sh``, shipped in
preset tarballs created on Windows, where the agent server has no POSIX shell
to run ``setup.sh`` with. Keep the two in step.

The SDK version is fetched from the automation service API on every run so
that deploying a new service version is the only step required to roll out a
new SDK. Standard library only: it runs before the virtual environment exists.
"""

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path


def _report_phase() -> None:
    """Best-effort progress phase for the dashboard; never fails the setup."""
    url = os.environ.get("AUTOMATION_PHASE_URL", "")
    token = os.environ.get("AUTOMATION_CALLBACK_API_KEY") or os.environ.get(
        "OPENHANDS_API_KEY", ""
    )
    if not url or not token:
        return
    request = urllib.request.Request(
        url,
        data=json.dumps({"phase": "Installing dependencies"}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=5).close()
    except Exception:
        pass


def main() -> int:
    print("[setup] Fetching SDK version from automation service", flush=True)
    version_url = f"{os.environ.get('AUTOMATION_API_URL', '')}/sdk-version"
    try:
        with urllib.request.urlopen(version_url, timeout=30) as response:
            sdk_version = json.load(response)["version"]
    except Exception:
        print(
            f"[setup] ERROR: Failed to fetch SDK version from {version_url}",
            file=sys.stderr,
        )
        return 1

    _report_phase()

    print("[setup] Creating isolated virtual environment", flush=True)
    # <3.14: otherwise uv may pick free-threaded 3.14t, where the SDK fails to
    # install.
    subprocess.run(
        ["uv", "venv", ".venv", "--python", "cpython>=3.12,<3.14", "--quiet"],
        check=True,
    )

    venv_python = Path(".venv/bin/python")
    if not venv_python.is_file():
        venv_python = Path(".venv/Scripts/python.exe")
    if not venv_python.is_file():
        print("[setup] ERROR: Python executable is missing from .venv", file=sys.stderr)
        return 1

    print(
        f"[setup] Installing OpenHands SDK from PyPI (version: {sdk_version})",
        flush=True,
    )
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(venv_python),
            "--quiet",
            f"openhands-sdk=={sdk_version}",
            f"openhands-tools=={sdk_version}",
            f"openhands-workspace=={sdk_version}",
        ],
        check=True,
    )

    # The entrypoint imports the SDK; verify installation without initializing it here.
    check = subprocess.run(
        [
            str(venv_python),
            "-c",
            "from importlib.metadata import version\n"
            "for package in ('openhands-sdk', 'openhands-tools', 'openhands-workspace'):\n"
            "    version(package)\n",
        ]
    )
    if check.returncode != 0:
        print(
            "[setup] ERROR: OpenHands SDK is not importable from .venv", file=sys.stderr
        )
        return 1

    print("[setup] Done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
