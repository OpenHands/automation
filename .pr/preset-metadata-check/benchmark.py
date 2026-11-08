"""Measure the actual base/head setup scripts against a live /sdk-version API.

No LLM is called. Generated virtual environments stay outside the repository.
Run: .venv/bin/python .pr/preset-metadata-check/benchmark.py --api-url URL
"""

import argparse
import json
import os
import platform
import re
import statistics
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = "506642221fa5255464024fc2c29dd8d5d883e31a"
SCRIPT = "openhands/automation/presets/prompt/setup.sh"
PACKAGES = ("openhands-sdk", "openhands-tools", "openhands-workspace")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", required=True)
    parser.add_argument(
        "--runtime", type=Path, default=Path("/private/tmp/oh668-runtime")
    )
    args = parser.parse_args()
    evidence = Path(__file__).resolve().parent
    args.runtime.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, AUTOMATION_API_URL=args.api_url)
    for name in (
        "AUTOMATION_PHASE_URL",
        "AUTOMATION_CALLBACK_API_KEY",
        "OPENHANDS_API_KEY",
    ):
        env.pop(name, None)
    before = subprocess.check_output(
        ["git", "show", f"{BASE}:{SCRIPT}"], cwd=ROOT, text=True
    )
    after = (ROOT / SCRIPT).read_text()
    sources = {"before": before, "after": after}
    timings = {"before": [], "after": []}

    def setup(label, source):
        work = args.runtime / label
        work.mkdir()  # Never overwrite a prior run's venv or evidence.
        script = work / "setup.sh"
        script.write_text(source)
        start = time.perf_counter()
        result = subprocess.run(
            ["bash", str(script)],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        elapsed = time.perf_counter() - start
        (evidence / f"{label}.log").write_text(result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError(f"{label} exited {result.returncode}; see its log")
        print(json.dumps({"setup": label, "seconds": elapsed}), flush=True)
        return elapsed, work

    setup("cache-warmup", after)
    last_fixed = None
    for index in range(3):
        order = ("before", "after") if index % 2 == 0 else ("after", "before")
        for kind in order:
            elapsed, work = setup(f"{kind}-{index + 1}", sources[kind])
            timings[kind].append(elapsed)
            if kind == "after":
                last_fixed = work
    assert last_fixed is not None
    python = last_fixed / ".venv/bin/python"
    checks = {}
    for kind, source in sources.items():
        match = re.search(r"""if ! "\$VENV_PYTHON" -c '(.*?)'; then""", source, re.S)
        assert match, kind
        samples = []
        for _ in range(3):
            start = time.perf_counter()
            subprocess.run(
                [str(python), "-c", match.group(1)],
                env=env,
                check=True,
                capture_output=True,
                timeout=120,
            )
            samples.append(time.perf_counter() - start)
        checks[kind] = samples
    versions = subprocess.check_output(
        [
            str(python),
            "-c",
            "import json,sys; from importlib.metadata import version; "
            f"print(json.dumps({{'python':sys.version,'packages':{{p:version(p) for p in {PACKAGES!r}}}}}))",
        ],
        env=env,
        text=True,
    )
    result = {
        "base": BASE,
        "platform": platform.platform(),
        "versions": json.loads(versions),
        "setup_seconds": timings,
        "setup_median_seconds": {k: statistics.median(v) for k, v in timings.items()},
        "standalone_check_seconds": checks,
        "standalone_check_median_seconds": {
            k: statistics.median(v) for k, v in checks.items()
        },
        "fixed_runtime": str(last_fixed),
        "method": "Three fresh venvs per variant, warm uv cache, alternating order. "
        "Standalone checks use the final fixed venv; the first base import "
        "there is cold, subsequent imports may use bytecode caches. No model calls.",
    }
    (evidence / "benchmark.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    main()
