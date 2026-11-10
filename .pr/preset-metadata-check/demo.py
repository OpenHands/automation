"""Read-only terminal demo of the actual before/after install checks.

Uses the already-installed benchmark venv. No service, install or model call.
This measures only the verification step, not complete setup.
"""

import argparse
import json
import re
import statistics
import subprocess
import time
from pathlib import Path

from benchmark import BASE, PACKAGES, ROOT, SCRIPT


def main():
    evidence = Path(__file__).resolve().parent
    recorded = json.loads((evidence / "benchmark.json").read_text())
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--python",
        type=Path,
        default=Path(recorded["fixed_runtime"]) / ".venv/bin/python",
        help="Interpreter with the three real SDK distributions installed",
    )
    args = parser.parse_args()
    if not args.python.is_file():
        parser.error("Benchmark venv is missing; pass --python or rerun benchmark.py.")

    versions = subprocess.check_output(
        [
            str(args.python),
            "-c",
            "from importlib.metadata import version; "
            f"print(', '.join(p+'='+version(p) for p in {PACKAGES!r}))",
        ],
        text=True,
    )
    print("Installed distributions:", versions.strip(), flush=True)
    before = subprocess.check_output(
        ["git", "show", f"{BASE}:{SCRIPT}"],
        cwd=ROOT,
        text=True,
    )
    sources = {
        "Before (imports)": before,
        "After (metadata)": (ROOT / SCRIPT).read_text(),
    }
    medians = {}
    for label, source in sources.items():
        match = re.search(r"""if ! "\$VENV_PYTHON" -c '(.*?)'; then""", source, re.S)
        if not match:
            raise RuntimeError(f"Cannot find the actual verification code: {label}")
        samples = []
        for _ in range(3):
            start = time.perf_counter()
            subprocess.run(
                [str(args.python), "-c", match.group(1)],
                check=True,
                capture_output=True,
                timeout=120,
            )
            samples.append(time.perf_counter() - start)
        medians[label] = statistics.median(samples)
        print(
            f"{label}: {medians[label]:.3f}s median; 3 samples "
            f"{[round(s, 3) for s in samples]}",
            flush=True,
        )
    print(
        "Both checks succeeded. The metadata check leaves SDK initialization "
        "to the entrypoint."
    )
    print(
        "Verification-only comparison; excludes package installation and model execution."
    )


if __name__ == "__main__":
    main()
