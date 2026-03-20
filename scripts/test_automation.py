#!/usr/bin/env python3
"""End-to-end dispatcher lifecycle test with live stdout streaming.

Simulates exactly what dispatcher._execute_run() does:
  1. Create sandbox  (dispatcher mints per-user API key first)
  2. Upload tarball  (dispatcher downloads from tarball_path)
  3. Start bash command (extract tar, run setup.sh, run entrypoint)
  4. Stream stdout/stderr in real-time via search_bash_events
  5. Delete sandbox

Uses start_bash_command + search_bash_events polling instead of
the blocking execute_bash_command so output appears on your local
terminal as it's produced inside the sandbox.

Usage
-----
    export OPENHANDS_API_KEY="sk-oh-..."
    python scripts/test_automation.py
    python scripts/test_automation.py --api-url https://staging.all-hands.dev
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

import httpx


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
log = logging.getLogger("test_automation")

DEFAULT_API_URL = "https://staging.all-hands.dev"
TARBALL_PATH = "/tmp/automation.tar.gz"
WORK_DIR = "/workspace/automation"
POLL_INTERVAL = 2.0
SANDBOX_POLL_INTERVAL = 5.0
SANDBOX_READY_TIMEOUT = 300.0
DEFAULT_TIMEOUT = 600


# -- helpers -------------------------------------------------------


def build_tarball(files: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            data = content.encode() if isinstance(content, str) else content
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _agent_headers(session_key: str) -> dict[str, str]:
    return {"X-Session-API-Key": session_key}


# -- sandbox lifecycle ---------------------------------------------


async def create_sandbox(
    client: httpx.AsyncClient,
    api_url: str,
    api_key: str,
) -> str:
    """POST /api/v1/sandboxes -> sandbox_id."""
    resp = await client.post(
        f"{api_url}/api/v1/sandboxes",
        headers=_headers(api_key),
    )
    resp.raise_for_status()
    sandbox_id = resp.json()["id"]
    log.info("Created sandbox %s", sandbox_id)
    return sandbox_id


async def wait_for_sandbox(
    client: httpx.AsyncClient,
    api_url: str,
    api_key: str,
    sandbox_id: str,
) -> tuple[str, str]:
    """Poll until RUNNING. Returns (session_api_key, agent_url)."""
    elapsed = 0.0
    while elapsed < SANDBOX_READY_TIMEOUT:
        resp = await client.get(
            f"{api_url}/api/v1/sandboxes",
            params={"id": sandbox_id},
            headers=_headers(api_key),
        )
        resp.raise_for_status()
        items = resp.json()
        if not items:
            raise RuntimeError(f"Sandbox {sandbox_id} disappeared")

        sb = items[0]
        status = sb.get("status", "UNKNOWN")
        log.info("Sandbox %s status: %s (%.0fs)", sandbox_id, status, elapsed)

        if status == "RUNNING":
            for url_info in sb.get("exposed_urls") or []:
                if url_info.get("name") == "AGENT_SERVER":
                    agent_url = url_info["url"].rstrip("/")
                    session_key = sb.get("session_api_key", "")
                    log.info("Sandbox ready -> %s", agent_url)
                    return session_key, agent_url
            raise RuntimeError(f"No AGENT_SERVER URL for {sandbox_id}")

        if status in ("ERROR", "MISSING"):
            raise RuntimeError(f"Sandbox {sandbox_id} -> {status}")

        await asyncio.sleep(SANDBOX_POLL_INTERVAL)
        elapsed += SANDBOX_POLL_INTERVAL

    raise TimeoutError(f"Sandbox {sandbox_id} not ready after {SANDBOX_READY_TIMEOUT}s")


async def upload_tarball(
    client: httpx.AsyncClient,
    agent_url: str,
    session_key: str,
    data: bytes,
) -> None:
    resp = await client.post(
        f"{agent_url}/api/file/upload/{TARBALL_PATH}",
        files={"file": ("upload", data)},
        headers=_agent_headers(session_key),
    )
    resp.raise_for_status()
    log.info("Uploaded tarball (%d bytes)", len(data))


async def start_command(
    client: httpx.AsyncClient,
    agent_url: str,
    session_key: str,
    command: str,
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """POST /api/bash/start_bash_command -> command_id.

    Non-blocking: returns immediately, command runs in background.
    """
    resp = await client.post(
        f"{agent_url}/api/bash/start_bash_command",
        json={"command": command, "timeout": timeout},
        headers=_agent_headers(session_key),
    )
    resp.raise_for_status()
    body = resp.json()
    cmd_id = body.get("id", "")
    log.info("Started command %s", cmd_id)
    return cmd_id


async def stream_output(
    client: httpx.AsyncClient,
    agent_url: str,
    session_key: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[int | None, str, str]:
    """Poll search_bash_events, print stdout live.

    Returns (exit_code, full_stdout, full_stderr).
    """
    headers = _agent_headers(session_key)
    last_order: int | None = None
    all_stdout: list[str] = []
    all_stderr: list[str] = []
    exit_code: int | None = None
    elapsed = 0.0

    while elapsed < timeout:
        params: dict[str, str] = {
            "kind": "BashOutput",
            "order_by": "TIMESTAMP",
        }
        if last_order is not None:
            params["min_order"] = str(last_order + 1)

        resp = await client.get(
            f"{agent_url}/api/bash/bash_events/search",
            params=params,
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", [])

        for item in items:
            order = item.get("order", 0)
            if last_order is None or order > last_order:
                last_order = order

            stdout = item.get("stdout") or ""
            stderr = item.get("stderr") or ""
            if stdout:
                sys.stdout.write(stdout)
                sys.stdout.flush()
                all_stdout.append(stdout)
            if stderr:
                sys.stderr.write(stderr)
                sys.stderr.flush()
                all_stderr.append(stderr)

            ec = item.get("exit_code")
            if ec is not None:
                exit_code = ec

        if exit_code is not None:
            break

        await asyncio.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL

    if exit_code is None and elapsed >= timeout:
        log.warning("Timed out after %.0fs", timeout)

    return exit_code, "".join(all_stdout), "".join(all_stderr)


async def delete_sandbox(
    client: httpx.AsyncClient,
    api_url: str,
    api_key: str,
    sandbox_id: str,
) -> None:
    try:
        resp = await client.delete(
            f"{api_url}/api/v1/sandboxes/{sandbox_id}",
            params={"sandbox_id": sandbox_id},
            headers=_headers(api_key),
        )
        if resp.status_code < 300:
            log.info("Deleted sandbox %s", sandbox_id)
        else:
            log.warning("Delete sandbox %s: %s", sandbox_id, resp.text)
    except Exception:
        log.exception("Error deleting sandbox %s", sandbox_id)


# -- the test ------------------------------------------------------


async def run_test(api_url: str, api_key: str) -> bool:
    """Full dispatcher lifecycle with live streaming.

    Mirrors dispatcher._execute_run():
      1. Create sandbox   (dispatcher uses per-user key)
      2. Upload tarball    (dispatcher downloads from tarball_path)
      3. Extract + setup.sh + entrypoint  (as one command)
      4. Stream stdout     (search_bash_events polling)
      5. Cleanup sandbox
    """
    api_url = api_url.rstrip("/")
    sandbox_id: str | None = None

    tarball = build_tarball(
        {
            "setup.sh": (
                "#!/bin/bash\necho '[setup] installing httpx'\npip install -q httpx\n"
            ),
            "main.py": "\n".join(
                [
                    "import os, httpx",
                    "",
                    "print(f'HTTPX={httpx.__version__}')",
                    "g = os.environ.get",
                    'print(f\'CALLBACK={g("AUTOMATION_CALLBACK_URL", "MISSING")}\')',
                    'print(f\'RUN_ID={g("AUTOMATION_RUN_ID", "MISSING")}\')',
                    'print(f\'SECRET={g("MY_SECRET", "MISSING")}\')',
                    "print('ALL_OK')",
                    "",
                ]
            ),
        }
    )

    # env vars the dispatcher would inject
    env_vars = {
        "MY_SECRET": "hunter2",
        "AUTOMATION_CALLBACK_URL": "https://example.com/callback",
        "AUTOMATION_RUN_ID": "test-run-001",
    }
    exports = " && ".join(f"export {k}='{v}'" for k, v in env_vars.items())
    entrypoint = "python main.py"
    cmd = (
        f"mkdir -p {WORK_DIR}"
        f" && tar xzf {TARBALL_PATH} -C {WORK_DIR}"
        f" && cd {WORK_DIR}"
        f" && ([ -f setup.sh ] && bash setup.sh || true)"
        f" && {exports}"
        f" && {entrypoint}"
    )

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            # 1. Create sandbox
            sandbox_id = await create_sandbox(client, api_url, api_key)

            # 2. Wait until running
            session_key, agent_url = await wait_for_sandbox(
                client, api_url, api_key, sandbox_id
            )

            # 3. Upload tarball
            await upload_tarball(client, agent_url, session_key, tarball)

            # 4. Start command (non-blocking)
            await start_command(client, agent_url, session_key, cmd)

            # 5. Stream output live to terminal
            log.info("--- sandbox stdout ---")
            exit_code, stdout, stderr = await stream_output(
                client, agent_url, session_key
            )
            log.info("--- end stdout (exit_code=%s) ---", exit_code)

            # 6. Verify
            ok = exit_code == 0 and "ALL_OK" in stdout
            if ok:
                log.info("PASS")
            else:
                log.error("FAIL")
                if stderr:
                    log.error("stderr: %s", stderr[:500])
            return ok

        except Exception:
            log.exception("Test failed")
            return False
        finally:
            if sandbox_id:
                await delete_sandbox(client, api_url, api_key, sandbox_id)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="E2E dispatcher lifecycle test with live stdout streaming",
    )
    parser.add_argument(
        "--api-url",
        default=os.environ.get("OPENHANDS_API_URL", DEFAULT_API_URL),
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("OPENHANDS_API_KEY", ""),
    )
    args = parser.parse_args()

    if not args.api_key:
        print("Set OPENHANDS_API_KEY or use --api-key", file=sys.stderr)
        sys.exit(1)

    log.info("API URL: %s", args.api_url)
    start = time.monotonic()
    ok = asyncio.run(run_test(args.api_url, args.api_key))
    elapsed = time.monotonic() - start
    log.info("Total time: %.1fs", elapsed)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
