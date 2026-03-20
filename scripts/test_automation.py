#!/usr/bin/env python3
"""End-to-end test for the automation dispatch logic.

Builds real tarballs and runs them in real sandboxes via run_automation().
Requires OPENHANDS_API_KEY to be set.

Usage:
    export OPENHANDS_API_KEY="sk-oh-..."
    python scripts/test_automation.py                          # default: echo test
    python scripts/test_automation.py --api-url https://staging.all-hands.dev
    python scripts/test_automation.py --test echo              # simple echo
    python scripts/test_automation.py --test setup-sh          # setup.sh + entrypoint
    python scripts/test_automation.py --test env-vars          # env var injection
    python scripts/test_automation.py --test sdk-import        # SDK import check
    python scripts/test_automation.py --test callback          # completion callback
    python scripts/test_automation.py --test all
"""

import argparse
import asyncio
import logging
import os
import sys
import time
import traceback


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "automation"))

from automation.execution import (  # noqa: E402
    AutomationResult,
    build_tarball,
    run_automation,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("test_automation")

DEFAULT_API_URL = "https://staging.all-hands.dev"


class TestResult:
    def __init__(self, name: str):
        self.name = name
        self.passed = False
        self.error: str | None = None
        self.duration: float = 0.0

    def __repr__(self) -> str:
        status = "✅ PASS" if self.passed else "❌ FAIL"
        return f"{status} {self.name} ({self.duration:.1f}s)"


def _log_result(r: AutomationResult) -> None:
    """Print the full bash execution result for debugging."""
    logger.info(
        "sandbox=%s  success=%s  exit_code=%s  error=%s",
        r.sandbox_id,
        r.success,
        r.exit_code,
        r.error,
    )
    if r.stdout:
        logger.info("--- stdout ---\n%s", r.stdout.rstrip())
    if r.stderr:
        logger.info("--- stderr ---\n%s", r.stderr.rstrip())


# -- Test cases ---------------------------------------------------------------


async def test_echo(api_url: str, api_key: str) -> TestResult:
    """Minimal: tarball with one script, no setup.sh."""
    result = TestResult("echo")
    start = time.monotonic()
    try:
        tarball = build_tarball(
            {
                "run.sh": '#!/bin/bash\necho "AUTOMATION_OK"',
            }
        )
        r = await run_automation(api_url, api_key, tarball, entrypoint="bash run.sh")
        _log_result(r)
        assert r.success, f"Expected success, got error: {r.error}"
        assert "AUTOMATION_OK" in r.stdout, (
            f"Missing marker in stdout: {r.stdout[:200]}"
        )
        result.passed = True
    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"
        logger.error("FAILED: %s", result.error)
        traceback.print_exc()
    finally:
        result.duration = time.monotonic() - start
    return result


async def test_setup_sh(api_url: str, api_key: str) -> TestResult:
    """Tarball with setup.sh that installs a package, entrypoint uses it."""
    result = TestResult("setup-sh")
    start = time.monotonic()
    try:
        tarball = build_tarball(
            {
                "setup.sh": "#!/bin/bash\npip install -q httpx\n",
                "run.py": (
                    'import httpx\nprint(f"SETUP_OK httpx={httpx.__version__}")\n'
                ),
            }
        )
        r = await run_automation(api_url, api_key, tarball, entrypoint="python run.py")
        _log_result(r)
        assert r.success, f"Expected success, got error: {r.error}"
        assert "SETUP_OK" in r.stdout, f"Missing marker in stdout: {r.stdout[:300]}"
        result.passed = True
    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"
        logger.error("FAILED: %s", result.error)
        traceback.print_exc()
    finally:
        result.duration = time.monotonic() - start
    return result


async def test_env_vars(api_url: str, api_key: str) -> TestResult:
    """Verify env_vars are available to the entrypoint."""
    result = TestResult("env-vars")
    start = time.monotonic()
    try:
        tarball = build_tarball(
            {
                "check_env.sh": '#!/bin/bash\necho "GOT_KEY=$MY_SECRET"',
            }
        )
        r = await run_automation(
            api_url,
            api_key,
            tarball,
            entrypoint="bash check_env.sh",
            env_vars={"MY_SECRET": "hunter2"},
        )
        _log_result(r)
        assert r.success, f"Expected success, got error: {r.error}"
        assert "GOT_KEY=hunter2" in r.stdout, (
            f"Env var not found in stdout: {r.stdout[:200]}"
        )
        result.passed = True
    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"
        logger.error("FAILED: %s", result.error)
        traceback.print_exc()
    finally:
        result.duration = time.monotonic() - start
    return result


async def test_sdk_import(api_url: str, api_key: str) -> TestResult:
    """Install SDK via setup.sh and verify OpenHandsCloudWorkspace is importable."""
    result = TestResult("sdk-import")
    start = time.monotonic()
    try:
        tarball = build_tarball(
            {
                "setup.sh": "#!/bin/bash\npip install -q openhands-workspace\n",
                "check.py": (
                    "from openhands.workspace import OpenHandsCloudWorkspace\n"
                    'print("SDK_IMPORT_OK")\n'
                ),
            }
        )
        r = await run_automation(
            api_url, api_key, tarball, entrypoint="python check.py"
        )
        _log_result(r)
        assert r.success, f"SDK import failed: {r.error}"
        assert "SDK_IMPORT_OK" in r.stdout, f"Missing marker: {r.stdout[:300]}"
        result.passed = True
    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"
        logger.error("FAILED: %s", result.error)
        traceback.print_exc()
    finally:
        result.duration = time.monotonic() - start
    return result


async def test_callback(api_url: str, api_key: str) -> TestResult:
    """Verify AUTOMATION_CALLBACK_URL and AUTOMATION_RUN_ID are injected as env vars."""
    result = TestResult("callback")
    start = time.monotonic()
    try:
        tarball = build_tarball(
            {
                "check.sh": (
                    "#!/bin/bash\n"
                    'echo "CB_URL=$AUTOMATION_CALLBACK_URL"\n'
                    'echo "RUN_ID=$AUTOMATION_RUN_ID"\n'
                ),
            }
        )
        r = await run_automation(
            api_url,
            api_key,
            tarball,
            entrypoint="bash check.sh",
            callback_url="https://example.com/api/v1/automations/runs/test-123/complete",
            run_id="test-123",
        )
        _log_result(r)
        assert r.success, f"Expected success, got: {r.error}"
        assert (
            "CB_URL=https://example.com/api/v1/automations/runs/test-123/complete"
            in r.stdout
        )
        assert "RUN_ID=test-123" in r.stdout
        result.passed = True
    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"
        logger.error("FAILED: %s", result.error)
        traceback.print_exc()
    finally:
        result.duration = time.monotonic() - start
    return result


# -- Runner -------------------------------------------------------------------

TEST_MAP = {
    "echo": test_echo,
    "setup-sh": test_setup_sh,
    "env-vars": test_env_vars,
    "sdk-import": test_sdk_import,
    "callback": test_callback,
}


async def run_tests(api_url: str, api_key: str, test_name: str) -> list[TestResult]:
    tests = list(TEST_MAP.values()) if test_name == "all" else [TEST_MAP[test_name]]
    results: list[TestResult] = []
    for test_fn in tests:
        logger.info("=" * 60)
        logger.info("Running: %s", test_fn.__name__)
        logger.info("=" * 60)
        r = await test_fn(api_url, api_key)
        results.append(r)
        logger.info("%s\n", r)
    return results


def main():
    parser = argparse.ArgumentParser(description="E2E test for automation dispatch")
    parser.add_argument(
        "--api-url", default=os.environ.get("OPENHANDS_API_URL", DEFAULT_API_URL)
    )
    parser.add_argument("--api-key", default=os.environ.get("OPENHANDS_API_KEY", ""))
    parser.add_argument("--test", default="echo", choices=list(TEST_MAP) + ["all"])
    args = parser.parse_args()

    if not args.api_key:
        print("Set OPENHANDS_API_KEY or use --api-key", file=sys.stderr)
        sys.exit(1)

    logger.info("API URL: %s", args.api_url)
    logger.info("Test:    %s\n", args.test)

    results = asyncio.run(run_tests(args.api_url, args.api_key, args.test))

    print("\n" + "=" * 60)
    print("RESULTS")
    print("=" * 60)
    for r in results:
        print(f"  {r}")
        if r.error:
            print(f"    Error: {r.error}")

    passed = sum(1 for r in results if r.passed)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
