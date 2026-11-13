"""Tests for the git sync CLI wrapper.

Uses real local repos (a bare "origin" plus clones), not mocked subprocesses.
"""

import asyncio
import subprocess

import pytest

import openhands.automation.git_sync.client as client_module
from openhands.automation.git_sync.client import (
    GitAuthError,
    GitSyncError,
    _auth_config_args,
    _looks_like_auth_failure,
    _non_interactive_env,
    _run_git,
    check_remote_access,
    commit_and_push,
    current_head,
    ensure_repo,
    pull,
)


@pytest.fixture
def origin(tmp_path):
    """A bare git repo acting as the remote."""
    origin_dir = tmp_path / "origin"
    origin_dir.mkdir()
    subprocess.run(
        ["git", "init", "--bare", "-q", "-b", "main"], cwd=origin_dir, check=True
    )
    return origin_dir


def _repo_url(path) -> str:
    return f"file://{path}"


def _ext_helper(tmp_path, name: str, stderr_line: str) -> list[str]:
    """Args that make `git` fail offline with a controlled stderr line.

    git's `ext::` transport runs an arbitrary command as the remote helper. A
    tiny script that prints to stderr and exits non-zero gives a real, non-mocked
    git failure with fully deterministic output -- no network, no fixture repo --
    so `_run_git`'s own failure path (not a monkeypatched stand-in) is exercised.
    """
    script = tmp_path / name
    script.write_text(f'#!/bin/sh\necho "{stderr_line}" >&2\nexit 128\n')
    script.chmod(0o755)
    return ["-c", "protocol.ext.allow=always", "ls-remote", f"ext::{script}"]


class TestEnsureRepo:
    async def test_clones_into_empty_workdir(self, tmp_path, origin):
        workdir = tmp_path / "clone"
        await ensure_repo(workdir, _repo_url(origin), "main", token="", timeout=30)
        assert (workdir / ".git").is_dir()

    async def test_noop_when_already_cloned(self, tmp_path, origin):
        workdir = tmp_path / "clone"
        await ensure_repo(workdir, _repo_url(origin), "main", token="", timeout=30)
        marker = workdir / ".git" / "marker"
        marker.write_text("keep me")
        await ensure_repo(workdir, _repo_url(origin), "main", token="", timeout=30)
        assert marker.exists()

    async def test_repoints_origin_when_repo_url_changes(self, tmp_path, origin):
        """A changed repo URL must repoint an existing checkout, not keep
        syncing against the stale origin."""
        other_origin = tmp_path / "other_origin"
        other_origin.mkdir()
        subprocess.run(
            ["git", "init", "--bare", "-q", "-b", "main"], cwd=other_origin, check=True
        )
        writer = tmp_path / "writer"
        await ensure_repo(writer, _repo_url(other_origin), "main", token="", timeout=30)
        (writer / "marker.txt").write_text("from other origin")
        await commit_and_push(
            writer, ".", "seed", "Bot", "bot@example.com", "main", "", 30
        )

        workdir = tmp_path / "clone"
        await ensure_repo(workdir, _repo_url(origin), "main", token="", timeout=30)
        current_url = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=workdir,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert current_url == _repo_url(origin)

        await ensure_repo(
            workdir, _repo_url(other_origin), "main", token="", timeout=30
        )
        new_url = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=workdir,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert new_url == _repo_url(other_origin)

        head = await pull(workdir, "main", token="", timeout=30)
        assert head is not None
        assert (workdir / "marker.txt").read_text() == "from other origin"


class TestPull:
    async def test_pull_on_brand_new_repo_returns_none(self, tmp_path, origin):
        workdir = tmp_path / "clone"
        await ensure_repo(workdir, _repo_url(origin), "main", token="", timeout=30)
        head = await pull(workdir, "main", token="", timeout=30)
        assert head is None

    async def test_pull_creates_local_branch_when_target_branch_is_new(
        self, tmp_path, origin
    ):
        """Overriding `branch` to a name that exists nowhere must create it
        locally, not stay on whatever was checked out."""
        workdir = tmp_path / "clone"
        await ensure_repo(workdir, _repo_url(origin), "main", token="", timeout=30)
        head = await pull(workdir, "feature-x", token="", timeout=30)
        assert head is None
        branch = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=workdir,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert branch == "feature-x"

    async def test_pull_picks_up_remote_changes(self, tmp_path, origin):
        writer = tmp_path / "writer"
        await ensure_repo(writer, _repo_url(origin), "main", token="", timeout=30)
        (writer / "file.txt").write_text("hello")
        pushed_sha = await commit_and_push(
            writer, ".", "add file", "Bot", "bot@example.com", "main", "", 30
        )

        reader = tmp_path / "reader"
        await ensure_repo(reader, _repo_url(origin), "main", token="", timeout=30)
        head = await pull(reader, "main", token="", timeout=30)

        assert head == pushed_sha
        assert (reader / "file.txt").read_text() == "hello"

    async def test_repointing_to_an_empty_origin_prunes_stale_remote_refs(
        self, tmp_path, origin
    ):
        """Switching to a repo with no commits yet must drop the previous
        remote's tracking refs.

        Without pruning, `origin/{branch}` survives pointing at the *old*
        remote's commit, so the cycle concludes it is already in sync and
        silently never pushes to the new remote -- with no error raised.
        """
        workdir = tmp_path / "clone"
        await ensure_repo(workdir, _repo_url(origin), "main", token="", timeout=30)
        (workdir / "file.txt").write_text("hi")
        seeded_sha = await commit_and_push(
            workdir, ".", "seed", "Bot", "bot@example.com", "main", "", 30
        )
        assert seeded_sha is not None

        # A brand-new remote with zero refs -- the case a stale ref hides.
        empty_origin = tmp_path / "empty_origin"
        empty_origin.mkdir()
        subprocess.run(
            ["git", "init", "--bare", "-q", "-b", "main"], cwd=empty_origin, check=True
        )

        await ensure_repo(
            workdir, _repo_url(empty_origin), "main", token="", timeout=30
        )
        await pull(workdir, "main", token="", timeout=30)

        stale_ref = subprocess.run(
            ["git", "rev-parse", "--verify", "origin/main"],
            cwd=workdir,
            capture_output=True,
            text=True,
        )
        assert stale_ref.returncode != 0, (
            "origin/main from the previous remote survived the repoint"
        )

        # The commit that only exists locally must now reach the new remote,
        # even though there is nothing new to stage.
        pushed = await commit_and_push(
            workdir, ".", "resync", "Bot", "bot@example.com", "main", "", 30
        )
        assert pushed == seeded_sha
        new_origin_head = subprocess.run(
            ["git", "rev-parse", "main"],
            cwd=empty_origin,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert new_origin_head == seeded_sha

    async def test_ff_only_divergence_raises(self, tmp_path, origin):
        writer_a = tmp_path / "writer_a"
        await ensure_repo(writer_a, _repo_url(origin), "main", token="", timeout=30)
        (writer_a / "a.txt").write_text("a")
        await commit_and_push(
            writer_a, ".", "add a", "Bot", "bot@example.com", "main", "", 30
        )

        writer_b = tmp_path / "writer_b"
        await ensure_repo(writer_b, _repo_url(origin), "main", token="", timeout=30)
        await pull(writer_b, "main", token="", timeout=30)
        (writer_b / "b.txt").write_text("b")
        subprocess.run(["git", "add", "."], cwd=writer_b, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Bot",
                "-c",
                "user.email=b@x.com",
                "commit",
                "-m",
                "b",
            ],
            cwd=writer_b,
            check=True,
        )

        # writer_a pushes again, so origin/main now has commits writer_b
        # doesn't know about *and* writer_b has a local commit not on origin.
        (writer_a / "a2.txt").write_text("a2")
        await commit_and_push(
            writer_a, ".", "add a2", "Bot", "bot@example.com", "main", "", 30
        )

        with pytest.raises(GitSyncError):
            await pull(writer_b, "main", token="", timeout=30)


class TestCommitAndPush:
    async def test_returns_none_when_nothing_to_commit(self, tmp_path, origin):
        workdir = tmp_path / "clone"
        await ensure_repo(workdir, _repo_url(origin), "main", token="", timeout=30)
        await pull(workdir, "main", token="", timeout=30)

        sha = await commit_and_push(
            workdir, ".", "noop", "Bot", "bot@example.com", "main", "", 30
        )
        assert sha is None

    async def test_retries_push_when_local_commit_was_never_pushed(
        self, tmp_path, origin
    ):
        """A commit that landed locally but failed to push must still go out
        on the next call, even with nothing new to stage."""
        workdir = tmp_path / "clone"
        await ensure_repo(workdir, _repo_url(origin), "main", token="", timeout=30)
        (workdir / "file.txt").write_text("hi")
        subprocess.run(["git", "add", "."], cwd=workdir, check=True)
        # Commit locally WITHOUT pushing: commit_and_push's commit step
        # succeeding and its push step failing right after.
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Bot",
                "-c",
                "user.email=bot@example.com",
                "commit",
                "-m",
                "unpushed commit",
            ],
            cwd=workdir,
            check=True,
        )
        unpushed_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=workdir,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        # Nothing new to stage: the working tree already matches the commit.
        sha = await commit_and_push(
            workdir, ".", "retry", "Bot", "bot@example.com", "main", "", 30
        )

        assert sha == unpushed_sha
        # And it must actually be on the remote now, not just locally.
        origin_head = subprocess.run(
            ["git", "rev-parse", "main"],
            cwd=origin,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert origin_head == unpushed_sha

    async def test_commits_and_pushes(self, tmp_path, origin):
        workdir = tmp_path / "clone"
        await ensure_repo(workdir, _repo_url(origin), "main", token="", timeout=30)
        (workdir / "file.txt").write_text("hi")

        sha = await commit_and_push(
            workdir, ".", "add file", "Bot", "bot@example.com", "main", "", 30
        )

        assert sha is not None
        assert sha == await current_head(workdir)

    async def test_only_stages_given_path(self, tmp_path, origin):
        workdir = tmp_path / "clone"
        await ensure_repo(workdir, _repo_url(origin), "main", token="", timeout=30)
        (workdir / "tracked").mkdir()
        (workdir / "tracked" / "file.txt").write_text("hi")
        (workdir / "untracked.txt").write_text("nope")

        sha = await commit_and_push(
            workdir,
            "tracked",
            "add tracked only",
            "Bot",
            "bot@example.com",
            "main",
            "",
            30,
        )

        assert sha is not None
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=workdir,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert "?? untracked.txt" in status
        assert "tracked/file.txt" not in status


class TestNonInteractive:
    async def test_run_git_passes_the_non_interactive_env_to_the_subprocess(
        self, tmp_path, origin, monkeypatch
    ):
        """Guards the wiring, not just the helper.

        Nothing can answer a git prompt in a background service, so a
        credential request blocks until the timeout burns the cycle. Dropping
        `env=` from the subprocess call would silently allow that again.
        """
        captured: dict[str, str] = {}
        real_run = subprocess.run

        def spy(args, **kwargs):
            captured.update(kwargs.get("env") or {})
            return real_run(args, **kwargs)

        monkeypatch.setattr(subprocess, "run", spy)

        workdir = tmp_path / "clone"
        await ensure_repo(workdir, _repo_url(origin), "main", token="", timeout=30)

        assert captured.get("GIT_TERMINAL_PROMPT") == "0"
        assert captured.get("GIT_ASKPASS") == ""
        assert "BatchMode=yes" in captured.get("GIT_SSH_COMMAND", "")

    def test_non_interactive_env_disables_every_prompt(self):
        env = _non_interactive_env()
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_ASKPASS"] == ""
        assert env["SSH_ASKPASS"] == ""
        assert "BatchMode=yes" in env["GIT_SSH_COMMAND"]
        # Still inherits the ambient environment (PATH, HOME, ...).
        assert "PATH" in env


class TestSubprocessRunner:
    """#491: clone/fetch over SSH stalled under `asyncio.create_subprocess_exec`."""

    async def test_clone_and_fetch_do_not_use_asyncio_subprocesses(
        self, tmp_path, origin, monkeypatch
    ):
        async def forbidden(*args, **kwargs):
            raise AssertionError("git must not run via asyncio.create_subprocess_exec")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)

        workdir = tmp_path / "clone"
        await ensure_repo(workdir, _repo_url(origin), "main", token="", timeout=30)
        await pull(workdir, "main", token="", timeout=30)

    async def test_a_slow_git_command_does_not_block_the_event_loop(
        self, tmp_path, monkeypatch
    ):
        hang = tmp_path / "hang.sh"
        hang.write_text("#!/bin/sh\nsleep 30\n")
        hang.chmod(0o755)
        monkeypatch.setenv("GIT_SSH_COMMAND", str(hang))
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.05)
                ticks += 1

        task = asyncio.create_task(ticker())
        try:
            with pytest.raises(GitSyncError, match="timed out after 1.0s"):
                await check_remote_access(
                    "ssh://git@example.invalid/org/repo.git", "main", "", timeout=1.0
                )
        finally:
            task.cancel()
        # The loop kept running while git waited in its worker thread.
        assert ticks >= 10


class TestCurrentHead:
    async def test_none_on_unborn_branch(self, tmp_path, origin):
        workdir = tmp_path / "clone"
        await ensure_repo(workdir, _repo_url(origin), "main", token="", timeout=30)
        assert await current_head(workdir) is None


class TestCheckRemoteAccess:
    """`POST /v1/git-sync/check` runs before a configuration is trusted, so it
    must answer without cloning, writing, or pushing anything."""

    async def test_reports_an_existing_branch(self, tmp_path, origin):
        writer = tmp_path / "writer"
        await ensure_repo(writer, _repo_url(origin), "main", token="", timeout=30)
        (writer / "file.txt").write_text("content")
        await commit_and_push(
            writer, ".", "msg", "Name", "a@b.c", "main", token="", timeout=30
        )

        assert (
            await check_remote_access(_repo_url(origin), "main", token="", timeout=30)
            is True
        )

    async def test_a_branch_that_does_not_exist_yet_is_not_a_failure(
        self, tmp_path, origin
    ):
        """The first sync creates the branch, so an absent one is fine."""
        assert (
            await check_remote_access(
                _repo_url(origin), "not-created-yet", token="", timeout=30
            )
            is False
        )

    async def test_raises_when_the_remote_cannot_be_reached(self, tmp_path):
        with pytest.raises(GitSyncError):
            await check_remote_access(
                _repo_url(tmp_path / "nothing-here"), "main", token="", timeout=30
            )

    async def test_leaves_no_checkout_behind(self, tmp_path, origin, monkeypatch):
        """`ls-remote` and nothing else: a check must not clone, and must not
        run any command that could write to the remote."""
        commands: list[list[str]] = []
        real_run = subprocess.run

        def spy(args, **kwargs):
            commands.append(list(args))
            return real_run(args, **kwargs)

        monkeypatch.setattr(subprocess, "run", spy)
        monkeypatch.chdir(tmp_path)

        await check_remote_access(_repo_url(origin), "main", token="", timeout=30)

        assert len(commands) == 1
        assert commands[0][1] == "ls-remote"
        assert list(tmp_path.iterdir()) == [origin]


class TestErrorClassification:
    def test_git_auth_error_is_a_git_sync_error(self):
        # Existing `except GitSyncError` handlers must still catch auth failures.
        assert issubclass(GitAuthError, GitSyncError)

    @pytest.mark.parametrize(
        "stderr",
        [
            "fatal: could not read Username for 'https://github.com': "
            "terminal prompts disabled",
            "remote: Support for password authentication was removed.\n"
            "fatal: Authentication failed for 'https://github.com/x/y.git/'",
            # GitHub over HTTPS: its own remote line plus git's.
            "remote: Repository not found.\n"
            "fatal: repository 'https://github.com/x/y.git/' not found",
            # GitLab: only git's own line names the 404.
            "remote: The project you were looking for could not be found or "
            "you don't have permission to view it.\n"
            "fatal: repository 'https://gitlab.com/x/y.git/' not found",
            # GitHub over SSH.
            "ERROR: Repository not found.\n"
            "fatal: Could not read from remote repository.",
            "fatal: unable to access '...': The requested URL returned error: 403",
            "git@github.com: Permission denied (publickey).",
        ],
    )
    def test_auth_failures_are_recognised(self, stderr):
        assert _looks_like_auth_failure(stderr) is True

    @pytest.mark.parametrize(
        "stderr",
        [
            "fatal: unable to access '...': Could not resolve host: github.com",
            "error: RPC failed; curl 56 recv failure: Connection reset by peer",
            "fatal: not a git repository (or any of the parent directories)",
            "fatal: the remote end hung up unexpectedly",
            # Local filesystem permissions: no token to fix.
            "fatal: could not create work tree dir '/workspace/x': Permission denied",
            "fatal: Unable to create '/workspace/x/.git/index.lock': Permission denied",
        ],
    )
    def test_transient_failures_are_not_misclassified(self, stderr):
        assert _looks_like_auth_failure(stderr) is False

    async def test_missing_local_repo_raises_transient_not_auth(
        self, tmp_path, monkeypatch
    ):
        """A clone of a nonexistent local repo is a plain GitSyncError, not a
        GitAuthError -- there is no credential problem to back off on."""
        monkeypatch.chdir(tmp_path)
        workdir = tmp_path / "clone"
        with pytest.raises(GitSyncError) as exc_info:
            await ensure_repo(
                workdir, _repo_url(tmp_path / "nope"), "main", token="", timeout=30
            )
        assert not isinstance(exc_info.value, GitAuthError)

    async def test_run_git_maps_auth_stderr_to_gitautherror(self, tmp_path):
        """The wiring seam itself: a real git failure whose stderr looks like an
        auth/access problem must come out of `_run_git` as `GitAuthError`, not a
        plain `GitSyncError`. The classifier is unit-tested above, but only this
        exercises the branch in `_run_git` that raises the distinct type -- the
        line the whole backoff feature hangs off. Uses git's `ext::` transport to
        drive a deterministic, offline non-zero exit with controlled stderr."""
        args = _ext_helper(
            tmp_path,
            "auth.sh",
            "fatal: Authentication failed for 'https://example.com/x.git'",
        )
        with pytest.raises(GitAuthError):
            await _run_git(args, cwd=None, timeout=30)

    async def test_run_git_keeps_transient_stderr_as_gitsyncerror(self, tmp_path):
        """The other side of the same seam: a genuine git failure with transient
        stderr must stay a plain `GitSyncError` and not be misrouted to the auth
        backoff path."""
        args = _ext_helper(
            tmp_path,
            "trans.sh",
            "fatal: unable to access: Could not resolve host",
        )
        with pytest.raises(GitSyncError) as exc_info:
            await _run_git(args, cwd=None, timeout=30)
        assert not isinstance(exc_info.value, GitAuthError)


class TestAuthScheme:
    """Basic auth as `x-access-token` is what GitHub, GitLab and Azure DevOps
    accept for a token; Bitbucket Data Center rejects that username but takes
    the token as a Bearer credential. The client falls back without any
    configuration and remembers what worked for the token."""

    REJECTED = (
        b"fatal: could not read Username for 'https://bitbucket.example.com': "
        b"terminal prompts disabled\n"
    )

    @pytest.fixture(autouse=True)
    def _forget_accepted_schemes(self):
        remembered = getattr(client_module, "_accepted_auth_scheme", {})
        remembered.clear()
        yield
        remembered.clear()

    @pytest.fixture
    def rejecting_exec(self, monkeypatch):
        """Run git for real, except that the listed auth schemes are refused the
        way a remote refuses them: a 401 turns into git's username prompt, which
        the non-interactive env converts into this exact stderr."""
        state = {"rejected": {"basic"}, "headers": []}
        real_run = subprocess.run

        def spy(args, **kwargs):
            headers = [
                arg.removeprefix("http.extraHeader=")
                for arg in args
                if isinstance(arg, str) and arg.startswith("http.extraHeader=")
            ]
            header = headers[0] if headers else None
            state["headers"].append(header)
            scheme = header.split()[1].lower() if header else None
            if scheme in state["rejected"]:
                return subprocess.CompletedProcess(
                    args, 128, b"", TestAuthScheme.REJECTED
                )
            stripped = [
                arg
                for index, arg in enumerate(args)
                if not (
                    (isinstance(arg, str) and arg.startswith("http.extraHeader="))
                    or (arg == "-c" and str(args[index + 1]).startswith("http.extra"))
                )
            ]
            return real_run(stripped, **kwargs)

        monkeypatch.setattr(subprocess, "run", spy)
        return state

    def test_basic_header_names_x_access_token(self):
        [flag, header] = _auth_config_args("tok")
        assert flag == "-c"
        assert (
            header == "http.extraHeader=AUTHORIZATION: basic eC1hY2Nlc3MtdG9rZW46dG9r"
        )

    def test_bearer_header_carries_the_token_itself(self):
        [_, header] = _auth_config_args("tok", client_module._AUTH_BEARER)
        assert header == "http.extraHeader=AUTHORIZATION: Bearer tok"

    def test_no_token_sends_no_header(self):
        assert _auth_config_args("") == []

    async def test_retries_with_bearer_when_basic_is_rejected(
        self, tmp_path, origin, rejecting_exec
    ):
        exists = await check_remote_access(
            _repo_url(origin), "main", token="dc-token", timeout=30
        )

        assert exists is False  # the empty bare origin has no branch yet
        assert rejecting_exec["headers"] == [
            "AUTHORIZATION: basic eC1hY2Nlc3MtdG9rZW46ZGMtdG9rZW4=",
            "AUTHORIZATION: Bearer dc-token",
        ]

    async def test_remembers_the_scheme_that_worked_for_the_token(
        self, tmp_path, origin, rejecting_exec
    ):
        await check_remote_access(
            _repo_url(origin), "main", token="dc-token", timeout=30
        )
        rejecting_exec["headers"].clear()

        workdir = tmp_path / "clone"
        await ensure_repo(
            workdir, _repo_url(origin), "main", token="dc-token", timeout=30
        )

        # The clone is the only command that carries the token; it went
        # straight to Bearer.
        assert [h for h in rejecting_exec["headers"] if h] == [
            "AUTHORIZATION: Bearer dc-token"
        ]

    async def test_a_different_token_starts_with_basic_again(
        self, tmp_path, origin, rejecting_exec
    ):
        await check_remote_access(
            _repo_url(origin), "main", token="dc-token", timeout=30
        )
        rejecting_exec["headers"].clear()
        rejecting_exec["rejected"] = set()

        await check_remote_access(
            _repo_url(origin), "main", token="gh-token", timeout=30
        )

        assert rejecting_exec["headers"] == [
            "AUTHORIZATION: basic eC1hY2Nlc3MtdG9rZW46Z2gtdG9rZW4="
        ]

    async def test_raises_the_first_attempts_auth_error_when_both_are_rejected(
        self, tmp_path, origin, rejecting_exec
    ):
        rejecting_exec["rejected"] = {"basic", "bearer"}

        with pytest.raises(GitAuthError) as exc_info:
            await check_remote_access(
                _repo_url(origin), "main", token="bad-token", timeout=30
            )

        assert "could not read Username" in str(exc_info.value)
        assert "bad-token" not in str(exc_info.value)
        assert len(rejecting_exec["headers"]) == 2
        assert client_module._accepted_auth_scheme == {}

    async def test_a_transient_failure_is_not_retried_with_another_scheme(
        self, tmp_path, rejecting_exec
    ):
        rejecting_exec["rejected"] = set()

        with pytest.raises(GitSyncError) as exc_info:
            await check_remote_access(
                _repo_url(tmp_path / "nothing-here"), "main", token="tok", timeout=30
            )

        assert not isinstance(exc_info.value, GitAuthError)
        assert len(rejecting_exec["headers"]) == 1

    async def test_without_a_token_no_scheme_is_tried(self, origin, rejecting_exec):
        await check_remote_access(_repo_url(origin), "main", token="", timeout=30)

        assert rejecting_exec["headers"] == [None]
