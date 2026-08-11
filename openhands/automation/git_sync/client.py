"""Async wrapper around the `git` CLI for the git sync feature.

Commands run via `asyncio.create_subprocess_exec` with an argument list
(never a shell string), so repo URLs/branch names can't be interpreted as
shell syntax. The auth token is passed per-invocation via
`-c http.extraHeader=...` rather than embedded in the remote URL or written
to `.git/config`, so it's never persisted to disk or logged.
"""

import asyncio
import base64
import logging
from pathlib import Path


logger = logging.getLogger("automation.git_sync")


class GitSyncError(Exception):
    """Raised when a git subprocess invocation fails or times out."""


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
    logged_args = ["git", *args]  # never includes the auth header
    logger.debug("Running: %s (cwd=%s)", " ".join(logged_args), cwd)

    try:
        proc = await asyncio.create_subprocess_exec(
            *full_args,
            cwd=str(cwd) if cwd else None,
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
        raise GitSyncError(
            f"git command failed ({proc.returncode}): {' '.join(logged_args)}\n"
            f"{stderr.decode(errors='replace').strip()}"
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
    await _run_git(["fetch", "origin"], cwd=workdir, token=token, timeout=timeout)

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
