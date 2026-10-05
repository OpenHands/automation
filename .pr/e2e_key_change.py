"""Drive the real automation service through the issue's #551 scenario.

Starts uvicorn in local mode (SQLite, local file store), creates a prompt
automation, syncs Git Sync to a local bare repo in plaintext, saves an
encryption key via PUT /v1/git-sync/config, syncs again and prints what the
branch HEAD holds. Run from the worktree with `uv run python <this file>`.
"""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx


PORT = int(os.environ.get("E2E_PORT", "18551"))
BASE = f"http://127.0.0.1:{PORT}/api/automation"
HEADERS = {"Authorization": "Bearer e2e-local-key"}


def log(msg: str = "") -> None:
    print(msg, flush=True)


def git(origin: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "--git-dir", str(origin), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def wait_for(predicate, what: str, timeout: float = 60.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.5)
    raise SystemExit(f"timed out waiting for {what}")


def sync_and_wait(client: httpx.Client, before_run_at: str | None) -> dict:
    r = client.post(f"{BASE}/v1/git-sync/sync")
    log(f"POST /v1/git-sync/sync -> {r.status_code} {r.json()}")

    def done():
        status = client.get(f"{BASE}/v1/git-sync/status").json()
        if (
            not status["sync_in_progress"]
            and status["last_synced_at"]
            and status["last_synced_at"] != before_run_at
        ):
            return status
        return None

    return wait_for(done, "sync to finish")


def show_head(origin: Path, slug_dir: str) -> None:
    log(f"  HEAD = {git(origin, 'rev-parse', '--short', 'main').strip()}")
    for name in ("automation.yaml", "tarball/prompt.txt"):
        content = git(origin, "show", f"main:{slug_dir}/{name}")
        first = content.splitlines()[0] if content else ""
        kind = "CIPHERTEXT" if content.startswith("gAAAAA") else "PLAINTEXT"
        log(f"  {name:<20} {kind:<10} first line: {first[:60]!r}")


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="e2e-551-"))
    origin = root / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(origin)], check=True)
    env = {
        **os.environ,
        "AUTOMATION_AGENT_SERVER_URL": "http://127.0.0.1:9",
        "AUTOMATION_LOCAL_API_KEY": "e2e-local-key",
        "AUTOMATION_DB_URL": f"sqlite+aiosqlite:///{root}/automation.db",
        "AUTOMATION_WORKSPACE_BASE": str(root / "workspace"),
        "AUTOMATION_GIT_SYNC_SECRET": "e2e-wrapping-secret",
        "FILE_STORE": "local",
        "LOCAL_STORAGE_PATH": str(root / "storage"),
        "AUTOMATION_SCHEDULER_ENABLED": "false",
        "LOG_LEVEL": "WARNING",
    }
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "openhands.automation.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(PORT),
        ],
        env=env,
        stdout=open(root / "server.log", "w"),
        stderr=subprocess.STDOUT,
    )
    try:
        client = httpx.Client(headers=HEADERS, timeout=30)

        def up():
            try:
                return client.get(f"{BASE}/v1/git-sync/status").status_code == 200
            except httpx.HTTPError:
                return False

        wait_for(up, "service to start")
        log(f"service up on :{PORT} (local mode, SQLite); origin = bare repo")
        log(f"commit under test: {subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], capture_output=True, text=True).stdout.strip()}")
        log()

        r = client.post(
            f"{BASE}/v1/preset/prompt",
            json={
                "name": "QA F24 Sync",
                "prompt": "Summarize yesterday's commits.",
                "model": "default",
                "trigger": {"type": "cron", "schedule": "0 9 * * 1", "timezone": "UTC"},
            },
        )
        log(f"POST /v1/preset/prompt -> {r.status_code} id={r.json().get('id')}")
        r.raise_for_status()

        r = client.put(
            f"{BASE}/v1/git-sync/config",
            json={"repo_url": f"file://{origin}", "branch": "main", "path": "qa-automations"},
        )
        log(f"PUT /v1/git-sync/config (repo, no key) -> {r.status_code} encryption_enabled={r.json()['encryption_enabled']}")
        status = sync_and_wait(client, None)
        log("1) first sync without a key:")
        show_head(origin, "qa-automations/qa-f24-sync")
        log()

        r = client.put(f"{BASE}/v1/git-sync/config", json={"encryption_key": "e2e-git-sync-key"})
        body = r.json()
        log(
            f"PUT /v1/git-sync/config {{encryption_key}} -> {r.status_code} "
            f"encryption_enabled={body['encryption_enabled']} dirty_count={body['dirty_count']}"
        )
        status = sync_and_wait(client, status["last_synced_at"])
        log(f"2) sync after saving the key: last_error={status['last_error']!r} dirty_count={status['dirty_count']}")
        show_head(origin, "qa-automations/qa-f24-sync")
        log()

        status = sync_and_wait(client, status["last_synced_at"])
        log("3) one more sync with nothing changed (expect no new commit):")
        show_head(origin, "qa-automations/qa-f24-sync")
        log()

        r = client.put(f"{BASE}/v1/git-sync/config", json={"encryption_key": None})
        body = r.json()
        log(
            f"PUT /v1/git-sync/config {{encryption_key: null}} -> {r.status_code} "
            f"encryption_enabled={body['encryption_enabled']} dirty_count={body['dirty_count']}"
        )
        status = sync_and_wait(client, status["last_synced_at"])
        log("4) sync after clearing the key:")
        show_head(origin, "qa-automations/qa-f24-sync")
        log()
        log("git log --oneline main:")
        log(git(origin, "log", "--oneline", "main").rstrip())
    finally:
        server.terminate()
        server.wait(timeout=20)


if __name__ == "__main__":
    main()
