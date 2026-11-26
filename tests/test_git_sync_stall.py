"""Git sync against a smart-HTTP remote that stalls its first transfer.

The stall is real: the server accepts the request and sends nothing, as a hung
remote or a dropped link does. Git's own HTTP transport helper is what holds
the connection, which is what the timeout has to get rid of.
"""

import asyncio
import subprocess
import threading
import time
from pathlib import Path

import pytest

import openhands.automation.git_sync.client as client_module
from openhands.automation.git_sync.client import GitSyncError, ensure_repo, pull
from tests.private_git_server import PrivateGitServer, _GitHandler, make_bare_repo


# How long the server holds a stalled request before giving up on its own.
_SERVER_STALL_SECONDS = 20


class _StallFirst(_GitHandler):
    stall_method = "GET"
    stalled: threading.Event
    release: threading.Event

    def _authorized_user(self) -> str | None:
        return "anon"

    def _serve(self) -> None:
        if self.command == self.stall_method and not self.stalled.is_set():
            self.stalled.set()
            self.release.wait(_SERVER_STALL_SECONDS)
            return
        super()._serve()


class _StallingServer(PrivateGitServer):
    def __init__(self, root: Path, method: str) -> None:
        super().__init__(root, "")
        self.release = threading.Event()
        self._httpd.RequestHandlerClass = type(
            "_Handler",
            (_StallFirst,),
            {
                "git_server": self,
                "stall_method": method,
                "stalled": threading.Event(),
                "release": self.release,
            },
        )

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def __exit__(self, *exc_info) -> None:
        self.release.set()
        super().__exit__(*exc_info)


def _git_processes_for(port: int) -> list[str]:
    ps = subprocess.run(
        ["ps", "-eo", "pid,ppid,args"], capture_output=True, text=True, check=True
    )
    return [line for line in ps.stdout.splitlines() if f"127.0.0.1:{port}/" in line]


@pytest.fixture
def checkout_of_stalling_fetch(tmp_path):
    """A complete checkout whose origin is a remote that stalls the next fetch."""
    make_bare_repo(tmp_path, "repo", {"automations/a/automation.yaml": "a\n"})
    with _StallingServer(tmp_path, "POST") as server:
        workdir = tmp_path / "checkout"
        subprocess.run(
            ["git", "clone", "-q", str(tmp_path / "repo.git"), str(workdir)],
            check=True,
        )
        subprocess.run(
            ["git", "remote", "set-url", "origin", f"{server.url}/repo.git"],
            cwd=workdir,
            check=True,
        )
        yield workdir, server


async def test_timeout_is_a_hard_bound_on_a_stalled_fetch(checkout_of_stalling_fetch):
    workdir, _ = checkout_of_stalling_fetch

    start = time.monotonic()
    with pytest.raises(GitSyncError, match="timed out after 2"):
        await pull(workdir, "main", "", timeout=2)

    elapsed = time.monotonic() - start
    assert elapsed < 6, f"pull(timeout=2) returned after {elapsed:.1f}s"


async def test_timeout_leaves_no_git_transport_helper(checkout_of_stalling_fetch):
    workdir, server = checkout_of_stalling_fetch

    task = asyncio.create_task(pull(workdir, "main", "", timeout=2))
    await asyncio.sleep(5)
    leftover = _git_processes_for(server.port)
    server.release.set()
    with pytest.raises(GitSyncError, match="timed out"):
        await task

    assert leftover == []


async def test_stalled_transfer_fails_before_the_timeout(
    checkout_of_stalling_fetch, monkeypatch
):
    workdir, _ = checkout_of_stalling_fetch
    monkeypatch.setattr(
        client_module,
        "_STALL_CONFIG_ARGS",
        ("-c", "http.lowSpeedLimit=1", "-c", "http.lowSpeedTime=2"),
    )

    start = time.monotonic()
    with pytest.raises(GitSyncError) as excinfo:
        await pull(workdir, "main", "", timeout=30)

    elapsed = time.monotonic() - start
    assert "timed out" not in str(excinfo.value)
    assert elapsed < 10, f"stalled fetch failed after {elapsed:.1f}s"


async def test_next_cycle_recovers_after_a_clone_timeout(tmp_path):
    sha = make_bare_repo(tmp_path, "repo", {"automations/a/automation.yaml": "a\n"})
    with _StallingServer(tmp_path, "GET") as server:
        url = f"{server.url}/repo.git"
        workdir = tmp_path / "checkout"
        with pytest.raises(GitSyncError, match="timed out"):
            await ensure_repo(workdir, url, "main", "", timeout=2)

        # The next cycle, with the remote healthy again.
        await ensure_repo(workdir, url, "main", "", timeout=30)
        assert await pull(workdir, "main", "", timeout=30) == sha


async def test_interrupted_clone_left_on_disk_is_cloned_again(tmp_path):
    """A checkout broken before the clone cleaned up after itself, e.g. by a
    restart mid-clone or an earlier release, is replaced on the next cycle."""
    sha = make_bare_repo(tmp_path, "repo", {"automations/a/automation.yaml": "a\n"})
    url = f"file://{tmp_path / 'repo.git'}"
    workdir = tmp_path / "checkout"
    workdir.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workdir, check=True)
    subprocess.run(["git", "remote", "add", "origin", url], cwd=workdir, check=True)
    subprocess.run(
        ["git", "config", "--unset", "remote.origin.fetch"], cwd=workdir, check=True
    )
    (workdir / ".git" / "HEAD").write_text("ref: refs/heads/.invalid\n")

    await ensure_repo(workdir, url, "main", "", timeout=30)

    assert await pull(workdir, "main", "", timeout=30) == sha


async def test_a_real_checkout_is_never_mistaken_for_an_interrupted_clone(tmp_path):
    sha = make_bare_repo(tmp_path, "repo", {"automations/a/automation.yaml": "a\n"})
    url = f"file://{tmp_path / 'repo.git'}"
    workdir = tmp_path / "checkout"
    subprocess.run(["git", "clone", "-q", url, str(workdir)], check=True)
    subprocess.run(
        ["git", "config", "--unset", "remote.origin.fetch"], cwd=workdir, check=True
    )
    (workdir / "notes.txt").write_text("keep me\n")

    await ensure_repo(workdir, url, "main", "", timeout=30)

    assert (workdir / "notes.txt").read_text() == "keep me\n"
    assert await client_module.current_head(workdir) == sha
