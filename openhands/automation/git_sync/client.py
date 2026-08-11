"""Async wrapper around the `git` CLI for the git sync feature.

Commands run via `asyncio.create_subprocess_exec` with an argument list
(never a shell string), so repo URLs/branch names can't be interpreted as
shell syntax. The auth token is passed per-invocation via
`-c http.extraHeader=...` rather than embedded in the remote URL or written
to `.git/config`, so it never reaches the checkout or a log line.

Credentials an operator embeds in the repo URL itself are a separate matter:
those are ordinary arguments, so `redact_url_credentials` strips them from
every message this module raises or logs. The configured token is still
persisted (encrypted) alongside the rest of the runtime config -- see
secret_store.py.
"""

import asyncio
import base64
import logging
import os
import re
from pathlib import Path


logger = logging.getLogger("automation.git_sync")

# The userinfo component of a URL: everything between "://" and "@".
_URL_CREDENTIALS_RE = re.compile(r"(?<=://)[^/@\s]+(?=@)")


class GitSyncError(Exception):
    """Raised when a git subprocess invocation fails or times out."""


def redact_url_credentials(text: str) -> str:
    """Blank out credentials embedded in any URL inside `text`.

    Passing a token via `-c http.extraHeader` keeps it out of argv, but
    nothing stops an operator from putting one in the repo URL itself
    ("https://x-access-token:ghp_xxx@github.com/org/repo.git"), which is a
    common way to authenticate. That URL is an ordinary argument to `clone`,
    and both the argv echoed in a GitSyncError and git's own stderr end up
    persisted in `git_sync_last_error` and rendered verbatim in the Git Sync
    page's error banner.
    """
    return _URL_CREDENTIALS_RE.sub("***", text)


def _non_interactive_env() -> dict[str, str]:
    """Environment for git subprocesses, with every prompt disabled.

    Nothing can answer a prompt here: this runs in a background service with
    no terminal attached, so an interactive credential request just blocks
    until the subprocess timeout expires and burns the whole cycle. With
    these set, missing or rejected credentials fail immediately with a real
    error message instead.

    Credential *helpers* still work -- this only suppresses git asking a
    human directly -- so a configured store/osxkeychain/manager helper keeps
    supplying credentials as before. A helper that blocks on its own GUI
    prompt is still only bounded by the subprocess timeout.
    """
    return {
        **os.environ,
        # Never prompt on the terminal ("Username for 'https://...'").
        "GIT_TERMINAL_PROMPT": "0",
        # Never shell out to a GUI askpass helper, including one inherited
        # from the parent environment.
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
        # Fail instead of asking for a host key or an SSH password.
        "GIT_SSH_COMMAND": os.environ.get("GIT_SSH_COMMAND", "ssh -o BatchMode=yes"),
    }


def _auth_config_args(token: str) -> list[str]:
    if not token:
        return []
    header = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return ["-c", f"http.extraHeader=AUTHORIZATION: basic {header}"]


async def _run_git(
    args: list[str],
    *,
    cwd: Path | None,
    timeout: float,
    token: str = "",
) -> str:
    """Run a git command, returning stdout. Raises GitSyncError on failure."""
    full_args = ["git", *_auth_config_args(token), *args]
    # Never includes the auth header, and any credentials embedded in a repo
    # URL argument are blanked out -- this string reaches the user-visible
    # error banner via `git_sync_last_error`.
    logged_args = ["git", *(redact_url_credentials(arg) for arg in args)]
    logger.debug("Running: %s (cwd=%s)", " ".join(logged_args), cwd)

    try:
        proc = await asyncio.create_subprocess_exec(
            *full_args,
            cwd=str(cwd) if cwd else None,
            env=_non_interactive_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        raise GitSyncError("git executable not found") from e

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise GitSyncError(
            f"git command timed out after {timeout}s: {' '.join(logged_args)}"
        ) from None

    if proc.returncode != 0:
        # git echoes the remote URL back in plenty of its own failure
        # messages, so stderr needs the same redaction as the argv.
        details = redact_url_credentials(stderr.decode(errors="replace").strip())
        raise GitSyncError(
            f"git command failed ({proc.returncode}): {' '.join(logged_args)}\n"
            f"{details}"
        )
    return stdout.decode(errors="replace")


async def current_head(workdir: Path) -> str | None:
    """Return the current HEAD commit sha, or `None` on an unborn branch
    (a freshly cloned/created repo with zero commits so far).
    """
    try:
        out = await _run_git(
            ["rev-parse", "--verify", "-q", "HEAD"], cwd=workdir, timeout=30.0
        )
    except GitSyncError:
        return None
    return out.strip() or None


async def diff_names(
    workdir: Path, path: str, base: str, head: str, timeout: float
) -> list[str]:
    """Return paths under `path` that changed between `base` and `head`.

    Raises GitSyncError if `base` isn't a valid/reachable commit in this
    checkout (e.g. a shallow clone or history rewrite) -- callers should
    fall back to treating everything under `path` as changed in that case.
    """
    out = await _run_git(
        ["diff", "--name-only", f"{base}..{head}", "--", path],
        cwd=workdir,
        timeout=timeout,
    )
    return [line for line in out.splitlines() if line.strip()]


async def _remote_branch_exists(workdir: Path, branch: str, timeout: float) -> bool:
    try:
        await _run_git(
            ["rev-parse", "--verify", f"origin/{branch}"], cwd=workdir, timeout=timeout
        )
    except GitSyncError:
        return False
    return True


async def _current_origin_url(workdir: Path, timeout: float) -> str | None:
    try:
        out = await _run_git(
            ["remote", "get-url", "origin"], cwd=workdir, timeout=timeout
        )
    except GitSyncError:
        return None
    return out.strip() or None


async def ensure_repo(
    workdir: Path, repo_url: str, branch: str, token: str, timeout: float
) -> None:
    """Clone `repo_url` into `workdir` if it isn't already a git checkout.

    If `workdir/.git` exists but `origin` points elsewhere (repo_url changed
    across a restart), repoints `origin` instead of silently syncing the old
    repo. Callers should call `pull()` afterward. Checks out `branch` if it
    exists on the remote, otherwise creates it locally.
    """
    if (workdir / ".git").is_dir():
        current_url = await _current_origin_url(workdir, timeout)
        if current_url != repo_url:
            logger.warning(
                "git-sync origin changed (%r -> %r); repointing existing "
                "checkout at %s",
                current_url,
                repo_url,
                workdir,
            )
            await _run_git(
                ["remote", "set-url", "origin", repo_url], cwd=workdir, timeout=timeout
            )
        return

    workdir.mkdir(parents=True, exist_ok=True)
    await _run_git(
        ["clone", "--origin", "origin", repo_url, "."],
        cwd=workdir,
        token=token,
        timeout=timeout,
    )

    if await _remote_branch_exists(workdir, branch, timeout):
        await _run_git(
            ["checkout", "-B", branch, f"origin/{branch}"], cwd=workdir, timeout=timeout
        )
    else:
        await _run_git(["checkout", "-b", branch], cwd=workdir, timeout=timeout)


async def pull(workdir: Path, branch: str, token: str, timeout: float) -> str | None:
    """Fast-forward the local `branch` to `origin/{branch}` and return HEAD.

    Returns `None` if there are no commits at all yet. Raises GitSyncError
    on a real divergence rather than force-pushing over it -- this loop is
    assumed to be the sole writer to `branch`.
    """
    # No refspec: naming `branch` explicitly would fail with "couldn't find
    # remote ref" on a brand-new repo that has no branches at all yet.
    #
    # `--prune` is load-bearing, not hygiene: `ensure_repo` can repoint origin
    # at a different repo (git_sync_repo_url changed at runtime), and without
    # pruning, the previous remote's tracking refs survive the switch. A stale
    # `origin/{branch}` makes `_remote_branch_exists` report True and
    # `_local_branch_ahead_of_remote` compute 0 commits ahead, so the cycle
    # concludes it is already in sync and silently never pushes to the new
    # remote -- with no error to surface.
    await _run_git(
        ["fetch", "--prune", "origin"], cwd=workdir, token=token, timeout=timeout
    )

    if not await _remote_branch_exists(workdir, branch, timeout):
        # Nothing pushed to this branch yet. If we're already on it (the
        # common case -- ensure_repo created it on first clone), stay put.
        # Otherwise (e.g. `branch` was just changed via a runtime config
        # override to a name that exists nowhere yet) create it locally,
        # branching off whatever's currently checked out.
        # symbolic-ref, not rev-parse --abbrev-ref: the latter fails with a
        # fatal error on an unborn branch (no commits yet), which is exactly
        # the state we're in here.
        current_branch = await _run_git(
            ["symbolic-ref", "--short", "HEAD"], cwd=workdir, timeout=timeout
        )
        if current_branch.strip() != branch:
            await _run_git(["checkout", "-B", branch], cwd=workdir, timeout=timeout)
        return await current_head(workdir)

    await _run_git(["checkout", branch], cwd=workdir, timeout=timeout)
    await _run_git(
        ["merge", "--ff-only", f"origin/{branch}"], cwd=workdir, timeout=timeout
    )
    return await current_head(workdir)


async def _local_branch_ahead_of_remote(
    workdir: Path, branch: str, timeout: float
) -> bool:
    """Whether local HEAD has commits `origin/{branch}` doesn't have yet.

    True after a prior cycle committed locally but its push failed -- that
    commit must still be pushed even when there's nothing new to stage.
    """
    if not await _remote_branch_exists(workdir, branch, timeout):
        return await current_head(workdir) is not None
    out = await _run_git(
        ["rev-list", "--count", f"origin/{branch}..HEAD"], cwd=workdir, timeout=timeout
    )
    return out.strip() != "0"


async def commit_and_push(
    workdir: Path,
    path: str,
    message: str,
    author_name: str,
    author_email: str,
    branch: str,
    token: str,
    timeout: float,
) -> str | None:
    """Stage `path`, commit if there are changes, and push. Returns the new
    HEAD sha, or `None` if there was nothing to commit and nothing pending
    to push.

    Also retries the push when local HEAD is already ahead of
    `origin/{branch}` with nothing new to stage -- covers a prior cycle
    that committed but failed to push.
    """
    # `git add -A -- <path>` errors if <path> doesn't exist in the working
    # tree at all (e.g. its last file was just removed). Only add when
    # there's something on disk to stage.
    if (workdir / path).exists():
        await _run_git(["add", "-A", "--", path], cwd=workdir, timeout=timeout)
    status = await _run_git(
        ["status", "--porcelain", "--", path], cwd=workdir, timeout=timeout
    )

    if status.strip():
        await _run_git(
            [
                "-c",
                f"user.name={author_name}",
                "-c",
                f"user.email={author_email}",
                "commit",
                "-m",
                message,
            ],
            cwd=workdir,
            timeout=timeout,
        )
    elif not await _local_branch_ahead_of_remote(workdir, branch, timeout):
        return None
    # else: nothing new to stage, but a prior cycle already committed
    # locally and its push failed -- fall through and retry the push.

    await _run_git(
        ["push", "origin", f"HEAD:{branch}"], cwd=workdir, token=token, timeout=timeout
    )
    return await current_head(workdir)
