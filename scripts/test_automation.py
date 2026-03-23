#!/usr/bin/env python3
"""End-to-end test for ``automation.execution.run_automation()``.

Exercises the **exact same code path** that the dispatcher uses,
without needing a database, cron job, or callback server.  This lets
you independently validate the execution layer against a live
OpenHands Cloud environment.

Usage
-----
    export OPENHANDS_API_KEY="sk-oh-..."
    python scripts/test_automation.py
    python scripts/test_automation.py --api-url https://staging.all-hands.dev
    python scripts/test_automation.py --tarball-dir ./my_custom_tarball
"""

from __future__ import annotations

import argparse
import asyncio
import io
import logging
import os
import sys
import tarfile
import time
from pathlib import Path

from automation.execution import AutomationResult, run_automation


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
log = logging.getLogger("test_automation")

DEFAULT_API_URL = "https://staging.all-hands.dev"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_TARBALL_DIR = SCRIPT_DIR / "test_tarball"


def build_tarball_from_dir(src: Path) -> bytes:
    """Pack a directory into a .tar.gz in memory."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in sorted(src.rglob("*")):
            if path.is_file():
                tar.add(path, arcname=path.relative_to(src))
    data = buf.getvalue()
    files = [str(p.relative_to(src)) for p in sorted(src.rglob("*")) if p.is_file()]
    log.info(
        "Built tarball from %s (%d bytes, %d files: %s)",
        src,
        len(data),
        len(files),
        files,
    )
    return data


def check_result(result: AutomationResult) -> bool:
    """Pretty-print the result and return True if the test passed."""
    log.info("=== RESULT ===")
    log.info("  success:    %s", result.success)
    log.info("  sandbox_id: %s", result.sandbox_id)
    log.info("  exit_code:  %s", result.exit_code)
    if result.error:
        log.error("  error:      %s", result.error)

    if result.stdout:
        log.info("--- stdout ---")
        for line in result.stdout.splitlines():
            log.info("  %s", line)

    if result.stderr:
        log.info("--- stderr (last 500 chars) ---")
        for line in result.stderr[-500:].splitlines():
            log.info("  %s", line)

    passed = result.success and "ALL_OK" in result.stdout
    log.info("PASS" if passed else "FAIL")
    return passed


async def run_test(
    api_url: str,
    api_key: str,
    tarball_dir: Path,
    entrypoint: str = "python main.py",
) -> bool:
    """Build a tarball and run it through ``run_automation()``."""
    tarball = build_tarball_from_dir(tarball_dir)

    env_vars = {
        "OPENHANDS_API_KEY": api_key,
        "OPENHANDS_CLOUD_API_URL": api_url,
    }

    result = await run_automation(
        api_url=api_url,
        api_key=api_key,
        tarball=tarball,
        entrypoint=entrypoint,
        env_vars=env_vars,
        callback_url="https://example.com/callback",
        run_id="test-run-001",
    )

    return check_result(result)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="E2E test for automation.execution.run_automation()",
    )
    parser.add_argument(
        "--api-url",
        default=os.environ.get("OPENHANDS_API_URL", DEFAULT_API_URL),
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("OPENHANDS_API_KEY", ""),
    )
    parser.add_argument(
        "--tarball-dir",
        type=Path,
        default=DEFAULT_TARBALL_DIR,
        help="Directory to pack into the tarball (default: scripts/test_tarball)",
    )
    parser.add_argument(
        "--entrypoint",
        default="python main.py",
        help="Command to run inside the sandbox (default: python main.py)",
    )
    args = parser.parse_args()

    if not args.api_key:
        print("Set OPENHANDS_API_KEY or use --api-key", file=sys.stderr)
        sys.exit(1)

    if not args.tarball_dir.is_dir():
        print(f"Tarball dir not found: {args.tarball_dir}", file=sys.stderr)
        sys.exit(1)

    log.info("API URL:      %s", args.api_url)
    log.info("Tarball dir:  %s", args.tarball_dir)
    log.info("Entrypoint:   %s", args.entrypoint)

    start = time.monotonic()
    ok = asyncio.run(
        run_test(args.api_url, args.api_key, args.tarball_dir, args.entrypoint)
    )
    elapsed = time.monotonic() - start
    log.info("Total time: %.1fs", elapsed)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
