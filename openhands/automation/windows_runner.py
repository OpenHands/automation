"""Start an automation run on a native Windows agent server.

On Windows the agent server runs commands through ``cmd.exe``, so the POSIX
command chain in ``execution.py`` (``mkdir -p``, ``tar``, ``. env``,
``bash setup.sh``) cannot run there. The automation service uploads this file
next to the run's tarball and a JSON spec, and starts it with
``uv run --no-project python <this file> <spec>``. It then does the same steps
in Python: load the private environment, extract the tarball, run the setup
script, run the entrypoint and exit with its code.

Standard library only: it runs under whichever Python ``uv`` picks.
"""

import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path


def run(spec_path: Path) -> int:
    """Run the automation described by *spec_path* and return its exit code."""
    # The spec holds injected secrets: read it and delete it before anything
    # else, as the POSIX chain does with its env file.
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec_path.unlink()

    work_dir = Path(spec["work_dir"])
    tarball = Path(spec["tarball"])
    work_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tarball, "r:gz") as tar:
        tar.extractall(work_dir, filter="data")
    tarball.unlink()

    env = {**os.environ, **spec["env"]}
    os.chdir(work_dir)

    if Path("setup.py").is_file():
        setup = subprocess.run([sys.executable, "setup.py"], env=env)
        if setup.returncode != 0:
            return setup.returncode
    elif Path("setup.sh").is_file():
        print(
            "setup.sh needs a POSIX shell, which a Windows agent server does not "
            "use; ship a setup.py instead",
            file=sys.stderr,
        )
        return 1

    # Entrypoints are validated to contain no shell metacharacters, so they
    # split into an argv without a shell. Resolve the program the way a shell
    # would, so commands such as ``uv`` or ``npm`` (a .cmd file) are found.
    argv = shlex.split(spec["entrypoint"])
    argv[0] = shutil.which(argv[0], path=env.get("PATH")) or argv[0]
    return subprocess.run(argv, env=env).returncode


if __name__ == "__main__":
    # The runner lives in the run's work directory; remove it so the agent
    # does not see it in its workspace.
    Path(__file__).unlink(missing_ok=True)
    sys.exit(run(Path(sys.argv[1])))
