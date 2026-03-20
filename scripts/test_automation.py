#!/usr/bin/env python3
"""End-to-end test simulating the dispatcher lifecycle.

Mirrors the exact flow in automation/dispatcher.py:
  1. Build tarball  (dispatcher downloads from tarball_path)
  2. Call run_automation() with callback_url + run_id
     (dispatcher calls this after minting a per-user API key)
  3. Verify sandbox result

Each test documents which dispatcher step it exercises.

Usage
-----
    export OPENHANDS_API_KEY="sk-oh-..."

    # Run a single lifecycle scenario
    python scripts/test_automation.py --test dispatch-basic

    # Run all scenarios
    python scripts/test_automation.py --test all

    # Override API URL (defaults to staging)
    python scripts/test_automation.py --api-url https://staging.all-hands.dev
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
import traceback
import uuid


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
    """Print the full sandbox execution result for debugging."""
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


# ---------------------------------------------------------------------------
# Test scenarios — each simulates a different dispatcher path
# ---------------------------------------------------------------------------


async def test_dispatch_basic(api_url: str, api_key: str) -> TestResult:
    """Simplest dispatch: tarball with a single script, no setup.sh.

    Simulates: dispatcher._execute_run() happy path where the
    automation has no setup_script_path and a trivial entrypoint.
    """
    result = TestResult("dispatch-basic")
    start = time.monotonic()
    try:
        # Step 1: build tarball (dispatcher downloads via _download_tarball)
        tarball = build_tarball({"main.py": 'print("DISPATCH_OK")'})

        # Step 2: run_automation (dispatcher calls this with per-user key)
        r = await run_automation(api_url, api_key, tarball, entrypoint="python main.py")
        _log_result(r)

        # Step 3: verify
        assert r.success, f"Expected success: {r.error}"
        assert "DISPATCH_OK" in r.stdout
        result.passed = True
    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"
        logger.error("FAILED: %s", result.error)
        traceback.print_exc()
    finally:
        result.duration = time.monotonic() - start
    return result


async def test_dispatch_with_setup(api_url: str, api_key: str) -> TestResult:
    """Dispatch with setup.sh — installs a dep, entrypoint uses it.

    Simulates: automation with setup_script_path="setup.sh".
    execution.py runs ``setup.sh`` before the entrypoint.
    """
    result = TestResult("dispatch-with-setup")
    start = time.monotonic()
    try:
        tarball = build_tarball(
            {
                "setup.sh": "#!/bin/bash\npip install -q httpx\n",
                "main.py": (
                    'import httpx\nprint(f"SETUP_OK httpx={httpx.__version__}")\n'
                ),
            }
        )
        r = await run_automation(api_url, api_key, tarball, entrypoint="python main.py")
        _log_result(r)
        assert r.success, f"Setup+run failed: {r.error}"
        assert "SETUP_OK" in r.stdout
        result.passed = True
    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"
        logger.error("FAILED: %s", result.error)
        traceback.print_exc()
    finally:
        result.duration = time.monotonic() - start
    return result


async def test_dispatch_env_injection(api_url: str, api_key: str) -> TestResult:
    """Verify env_vars are exported before the entrypoint.

    Simulates: dispatcher injecting AUTOMATION_EVENT_PAYLOAD and
    any other env vars into the sandbox execution context.
    """
    result = TestResult("dispatch-env-injection")
    start = time.monotonic()
    try:
        tarball = build_tarball({"main.sh": '#!/bin/bash\necho "SECRET=$MY_SECRET"'})
        r = await run_automation(
            api_url,
            api_key,
            tarball,
            entrypoint="bash main.sh",
            env_vars={"MY_SECRET": "hunter2"},
        )
        _log_result(r)
        assert r.success, f"Env injection failed: {r.error}"
        assert "SECRET=hunter2" in r.stdout
        result.passed = True
    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"
        logger.error("FAILED: %s", result.error)
        traceback.print_exc()
    finally:
        result.duration = time.monotonic() - start
    return result


async def test_dispatch_callback_env(api_url: str, api_key: str) -> TestResult:
    """Verify callback_url and run_id are injected as env vars.

    Simulates: dispatcher._execute_run() constructing the callback
    URL and passing it to run_automation(). The SDK reads these to
    POST completion status back to the automation service.
    """
    result = TestResult("dispatch-callback-env")
    start = time.monotonic()
    fake_run_id = str(uuid.uuid4())
    callback_url = (
        f"https://automation.staging.all-hands.dev"
        f"/api/v1/automations/runs/{fake_run_id}/complete"
    )
    try:
        tarball = build_tarball(
            {
                "main.sh": (
                    "#!/bin/bash\n"
                    'echo "CB=$AUTOMATION_CALLBACK_URL"\n'
                    'echo "RUN=$AUTOMATION_RUN_ID"\n'
                ),
            }
        )
        r = await run_automation(
            api_url,
            api_key,
            tarball,
            entrypoint="bash main.sh",
            callback_url=callback_url,
            run_id=fake_run_id,
        )
        _log_result(r)
        assert r.success, f"Callback env test failed: {r.error}"
        assert f"CB={callback_url}" in r.stdout
        assert f"RUN={fake_run_id}" in r.stdout
        result.passed = True
    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"
        logger.error("FAILED: %s", result.error)
        traceback.print_exc()
    finally:
        result.duration = time.monotonic() - start
    return result


async def test_dispatch_sdk_workspace(api_url: str, api_key: str) -> TestResult:
    """Install SDK, verify OpenHandsCloudWorkspace is importable.

    Simulates: the real-world scenario where the tarball contains
    an SDK script that uses OpenHandsCloudWorkspace in local mode.
    """
    result = TestResult("dispatch-sdk-workspace")
    start = time.monotonic()
    try:
        tarball = build_tarball(
            {
                "setup.sh": ("#!/bin/bash\npip install -q openhands-workspace\n"),
                "main.py": (
                    "from openhands.workspace "
                    "import OpenHandsCloudWorkspace\n"
                    'print("SDK_OK")\n'
                ),
            }
        )
        r = await run_automation(api_url, api_key, tarball, entrypoint="python main.py")
        _log_result(r)
        assert r.success, f"SDK import failed: {r.error}"
        assert "SDK_OK" in r.stdout
        result.passed = True
    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"
        logger.error("FAILED: %s", result.error)
        traceback.print_exc()
    finally:
        result.duration = time.monotonic() - start
    return result


async def test_dispatch_failure(api_url: str, api_key: str) -> TestResult:
    """Entrypoint exits non-zero — dispatcher should see failure.

    Simulates: dispatcher handling a failed run.  The _execute_run()
    path calls _mark_run_failed() when result.success is False.
    """
    result = TestResult("dispatch-failure")
    start = time.monotonic()
    try:
        tarball = build_tarball({"main.sh": "#!/bin/bash\nexit 42\n"})
        r = await run_automation(api_url, api_key, tarball, entrypoint="bash main.sh")
        _log_result(r)
        assert not r.success, "Expected failure, got success"
        assert r.exit_code == 42, f"Expected exit 42, got {r.exit_code}"
        result.passed = True
    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"
        logger.error("FAILED: %s", result.error)
        traceback.print_exc()
    finally:
        result.duration = time.monotonic() - start
    return result


async def test_dispatch_full_lifecycle(api_url: str, api_key: str) -> TestResult:
    """Full dispatcher lifecycle: setup → entrypoint → callback env → output.

    Combines all dispatcher steps in a single run to simulate the
    complete happy path: tarball with setup.sh, env vars, callback
    env injection, and SDK availability — all verified in one shot.
    """
    result = TestResult("dispatch-full-lifecycle")
    start = time.monotonic()
    fake_run_id = str(uuid.uuid4())
    callback_url = (
        f"https://automation.staging.all-hands.dev"
        f"/api/v1/automations/runs/{fake_run_id}/complete"
    )
    try:
        tarball = build_tarball(
            {
                "setup.sh": ("#!/bin/bash\npip install -q httpx\n"),
                "main.py": "\n".join(
                    [
                        "import os, httpx",
                        'print(f"HTTPX={httpx.__version__}")',
                        "g = os.environ.get",
                        "print(f\"SECRET={g('MY_SECRET', 'X')}\")",
                        "print(f\"CB={g('AUTOMATION_CALLBACK_URL', 'X')}\")",
                        "print(f\"RUN={g('AUTOMATION_RUN_ID', 'X')}\")",
                        'print("LIFECYCLE_OK")',
                        "",
                    ]
                ),
            }
        )
        r = await run_automation(
            api_url,
            api_key,
            tarball,
            entrypoint="python main.py",
            env_vars={"MY_SECRET": "s3cret"},
            callback_url=callback_url,
            run_id=fake_run_id,
        )
        _log_result(r)
        assert r.success, f"Lifecycle test failed: {r.error}"
        assert "LIFECYCLE_OK" in r.stdout
        assert "SECRET=s3cret" in r.stdout
        assert f"CB={callback_url}" in r.stdout
        assert f"RUN={fake_run_id}" in r.stdout
        # httpx was installed by setup.sh
        assert "HTTPX=" in r.stdout
        result.passed = True
    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"
        logger.error("FAILED: %s", result.error)
        traceback.print_exc()
    finally:
        result.duration = time.monotonic() - start
    return result


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TEST_MAP: dict[str, object] = {
    "dispatch-basic": test_dispatch_basic,
    "dispatch-with-setup": test_dispatch_with_setup,
    "dispatch-env-injection": test_dispatch_env_injection,
    "dispatch-callback-env": test_dispatch_callback_env,
    "dispatch-sdk-workspace": test_dispatch_sdk_workspace,
    "dispatch-failure": test_dispatch_failure,
    "dispatch-full-lifecycle": test_dispatch_full_lifecycle,
}


async def run_tests(api_url: str, api_key: str, test_name: str) -> list[TestResult]:
    if test_name == "all":
        tests = list(TEST_MAP.values())
    else:
        tests = [TEST_MAP[test_name]]

    results: list[TestResult] = []
    for test_fn in tests:
        logger.info("=" * 60)
        logger.info("Running: %s", test_fn.__name__)  # type: ignore[union-attr]
        logger.info("=" * 60)
        r = await test_fn(api_url, api_key)  # type: ignore[operator]
        results.append(r)
        logger.info("%s\n", r)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="E2E test simulating dispatcher lifecycle"
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
        "--test",
        default="dispatch-basic",
        choices=list(TEST_MAP) + ["all"],
    )
    args = parser.parse_args()

    if not args.api_key:
        print(
            "Set OPENHANDS_API_KEY or use --api-key",
            file=sys.stderr,
        )
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
