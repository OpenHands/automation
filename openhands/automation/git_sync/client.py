"""Async wrapper around the `git` CLI.

Commands run via `create_subprocess_exec` with an argument list, never a shell
string. The auth token goes per-invocation through `-c http.extraHeader`, so it
reaches neither the checkout nor a log line; credentials an operator embeds in
the repo URL are ordinary arguments, so `redact_url_credentials` strips those
from everything this module raises or logs. The token itself is persisted
encrypted -- see secret_store.py.
"""

import asyncio
import base64
import logging
import os
import re
from pathlib import Path
from typing import Final


logger = logging.getLogger("automation.git_sync")

# The userinfo component of a URL: everything between "://" and "@".
_URL_CREDENTIALS_RE: Final[re.Pattern[str]] = re.compile(r"(?<=://)[^/@\s]+(?=@)")


class GitSyncError(Exception):
    """Raised when a git subprocess invocation fails or times out."""


def redact_url_credentials(text: str) -> str:
    """Blank out credentials embedded in any URL inside `text`.

    An operator may authenticate by putting the token in the repo URL
    ("https://x-access-token:ghp_xxx@github.com/org/repo.git"). That URL is an
    ordinary argument, and both the argv in a GitSyncError and git's stderr
    land in `git_sync_last_error`, shown verbatim in the UI's error banner.
    """
    return _URL_CREDENTIALS_RE.sub("***", text)


def _non_interactive_env() -> dict[str, str]:
    """Environment for git subprocesses, with every prompt disabled.

    Nothing can answer a prompt in a background service with no terminal, so a
    credential request would just block until the subprocess timeout burns the
    cycle. Bad credentials now fail immediately with a real error instead.

    Credential *helpers* still work; this only stops git asking a human.
    """
    return {
        **os.environ,
        # Never prompt on the terminal ("Username for 'https://...'").
        "GIT_TERMINAL_PROMPT": "0",
        # Never shell out to a GUI askpass helper, inherited ones included.
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
    # Excludes the auth header and redacts URL credentials: this string reaches
    # the user-visible error banner via `git_sync_last_error`.
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
        # git echoes the remote URL back in its own failure messages, so stderr
        # needs the same redaction as the argv.
        details = redact_url_credentials(stderr.decode(errors="replace").strip())
        raise GitSyncError(
            f"git command failed ({proc.returncode}): {' '.join(logged_args)}\n"
            f"{details}"
        )
    return stdout.decode(errors="replace")


async def check_remote_access(
    repo_url: str, branch: str, token: str, timeout: float
) -> bool:
    """Whether `branch` already exists on `repo_url`; raises if it can't ask.

    `ls-remote` and nothing else: this runs before a configuration is trusted,
    so it must not be a sync. A cycle would import whatever it finds and push
    every dirty automation back -- against a mistyped URL that is the damage,
    not the diagnosis. `ls-remote` writes nothing and still catches what a typo
    breaks: host unreachable, credentials rejected, repo absent.

    Proves read access only; a token with no write scope still fails at push.
    A missing branch returns False rather than raising -- `ensure_repo`/`pull`
    create it.
    """
    # `--` so a repo URL beginning with a dash can't be read as an option
    # (`--upload-pack=...` would run a command of the caller's choosing).
    out = await _run_git(
        ["ls-remote", "--heads", "--", repo_url, branch],
        cwd=None,
        token=token,
        timeout=timeout,
    )
    return bool(out.strip())


async def current_head(workdir: Path) -> str | None:
    """The current HEAD sha, or `None` on an unborn branch (zero commits)."""
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
    """Paths under `path` that changed between `base` and `head`.

    Raises GitSyncError if `base` isn't reachable in this checkout (shallow
    clone, history rewrite); callers should then treat everything as changed.
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

    If `workdir/.git` exists but `origin` points elsewhere (repo_url changed),
    repoints `origin` rather than silently syncing the old repo. Checks out
    `branch`, creating it locally if the remote doesn't have it. Callers should
    `pull()` afterward.
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

    `None` if there are no commits yet. Raises GitSyncError on a real
    divergence rather than force-pushing over it -- this loop is assumed to be
    the sole writer to `branch`.
    """
    # No refspec: naming `branch` would fail with "couldn't find remote ref" on
    # a brand-new repo with no branches.
    #
    # `--prune` is load-bearing. `ensure_repo` can repoint origin at a
    # different repo, and the old remote's tracking refs survive the switch. A
    # stale `origin/{branch}` makes the cycle conclude it is already in sync,
    # so it silently never pushes to the new remote -- with no error raised.
    await _run_git(
        ["fetch", "--prune", "origin"], cwd=workdir, token=token, timeout=timeout
    )

    if not await _remote_branch_exists(workdir, branch, timeout):
        # Nothing pushed to this branch yet. Stay put if already on it (the
        # common case), else create it locally off the current checkout --
        # `branch` may have just been repointed at a name that exists nowhere.
        # symbolic-ref, not rev-parse --abbrev-ref: the latter is fatal on an
        # unborn branch, which is exactly the state here.
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
    """Whether local HEAD has commits `origin/{branch}` doesn't.

    True after a prior cycle committed but failed to push; that commit still
    needs pushing even with nothing new to stage.
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
    """Stage `path`, commit if changed, and push; returns the new HEAD sha.

    `None` when there was nothing to commit and nothing pending to push. Also
    retries the push when HEAD is already ahead of `origin/{branch}` with
    nothing new to stage -- a prior cycle that committed but failed to push.
    """
    # `git add -A -- <path>` errors if <path> is absent from the working tree
    # (e.g. its last file was just removed), so only add when it exists.
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
    # else: nothing to stage, but a prior cycle's commit never pushed -- fall
    # through and retry it.

    await _run_git(
        ["push", "origin", f"HEAD:{branch}"], cwd=workdir, token=token, timeout=timeout
    )
    return await current_head(workdir)
