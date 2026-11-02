"""Tests for windows_runner.py, the run bootstrap used on Windows agent servers.

The runner is plain Python, so these tests run it the way the agent server
does (``python <runner> <spec>``) on any OS.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from openhands.automation import windows_runner
from openhands.automation.execution import build_tarball


def _stage(
    work_dir: Path, files: dict[str, str | bytes], entrypoint: str, env: dict
) -> Path:
    """Lay out a run the way execute_in_context does and return the runner."""
    work_dir.mkdir(parents=True)
    tarball = work_dir / ".automation-run.tar.gz"
    tarball.write_bytes(build_tarball(files))
    spec = work_dir / ".automation-run.json"
    spec.write_text(
        json.dumps(
            {
                "work_dir": str(work_dir),
                "tarball": str(tarball),
                "entrypoint": entrypoint,
                "env": env,
            }
        )
    )
    runner = work_dir / ".automation-run.py"
    shutil.copyfile(windows_runner.__file__, runner)
    return runner


def _run(runner: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(runner), str(runner.with_suffix(".json"))],
        capture_output=True,
        text=True,
        timeout=60,
    )


# The entrypoint resolves ``python`` from the injected PATH, as a preset's
# ``.venv/Scripts/python.exe`` would resolve from the work dir.
_PATH = os.pathsep.join([str(Path(sys.executable).parent), os.environ["PATH"]])


def test_runs_setup_then_entrypoint_with_private_env(tmp_path):
    work_dir = tmp_path / "automation-runs" / "run-1"
    runner = _stage(
        work_dir,
        {
            "setup.sh": "exit 1\n",
            "setup.py": "open('setup-ran', 'w').close()\n",
            "main.py": (
                "import os, pathlib, sys\n"
                "assert pathlib.Path('setup-ran').exists()\n"
                "pathlib.Path('out.txt').write_text(os.environ['PRIVATE_VALUE'])\n"
                "sys.exit(3)\n"
            ),
        },
        entrypoint="python main.py",
        env={"PATH": _PATH, "PRIVATE_VALUE": "it's $private"},
    )

    result = _run(runner)

    assert result.returncode == 3, result.stderr
    assert (work_dir / "out.txt").read_text() == "it's $private"
    # Nothing the service staged is left in the agent's workspace.
    assert not runner.exists()
    assert not (work_dir / ".automation-run.json").exists()
    assert not (work_dir / ".automation-run.tar.gz").exists()


def test_failed_setup_stops_the_run(tmp_path):
    work_dir = tmp_path / "run"
    runner = _stage(
        work_dir,
        {
            "setup.py": "raise SystemExit(7)\n",
            "main.py": "open('main-ran', 'w').close()\n",
        },
        entrypoint="python main.py",
        env={"PATH": _PATH},
    )

    result = _run(runner)

    assert result.returncode == 7
    assert not (work_dir / "main-ran").exists()


def test_setup_sh_alone_fails_with_a_clear_message(tmp_path):
    work_dir = tmp_path / "run"
    runner = _stage(
        work_dir,
        {"setup.sh": "pip install x\n", "main.py": "open('main-ran', 'w').close()\n"},
        entrypoint="python main.py",
        env={"PATH": _PATH},
    )

    result = _run(runner)

    assert result.returncode == 1
    assert "setup.py" in result.stderr
    assert not (work_dir / "main-ran").exists()
