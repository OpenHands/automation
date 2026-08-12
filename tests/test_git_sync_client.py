"""Tests for the git sync CLI wrapper.

Uses real local git repos (a bare "origin" plus working clones) instead of
mocking subprocess calls.
"""

import asyncio
import subprocess

import pytest

from openhands.automation.git_sync.client import (
    GitSyncError,
    _non_interactive_env,
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
        """An existing checkout must repoint, not silently keep syncing
        against a stale origin, when AUTOMATION_GIT_SYNC_REPO_URL changes."""
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
        """Overriding `branch` to a name that exists nowhere yet must create
        it locally, not silently stay on whatever branch was checked out."""
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
        """Switching `git_sync_repo_url` to a repo that has no commits yet
        must drop the previous remote's tracking refs.

        Without pruning, `origin/{branch}` survives the switch still pointing
        at the *old* remote's commit. That makes `_remote_branch_exists`
        report True and `_local_branch_ahead_of_remote` compute 0 commits
        ahead, so the cycle concludes it is already in sync and silently
        never pushes to the new remote -- with no error to surface.
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
        """A commit that landed locally but failed to push (e.g. network
        blip) must still get pushed on the next call, even with nothing new
        to stage."""
        workdir = tmp_path / "clone"
        await ensure_repo(workdir, _repo_url(origin), "main", token="", timeout=30)
        (workdir / "file.txt").write_text("hi")
        subprocess.run(["git", "add", "."], cwd=workdir, check=True)
        # Commit locally WITHOUT pushing -- simulates commit_and_push's
        # commit step succeeding and the push step failing right after.
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

        # Nothing new to stage this time -- the working tree already
        # matches the unpushed commit.
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

        Nothing can answer a git prompt in a background service with no
        terminal, so an interactive credential request blocks until the
        subprocess timeout expires and burns the whole cycle. Dropping
        `env=` from the subprocess call would silently allow that again.
        """
        captured: dict[str, str] = {}
        real_exec = asyncio.create_subprocess_exec

        async def spy(*args, **kwargs):
            captured.update(kwargs.get("env") or {})
            return await real_exec(*args, **kwargs)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)

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


class TestCurrentHead:
    async def test_none_on_unborn_branch(self, tmp_path, origin):
        workdir = tmp_path / "clone"
        await ensure_repo(workdir, _repo_url(origin), "main", token="", timeout=30)
        assert await current_head(workdir) is None


class TestCheckRemoteAccess:
    """`POST /v1/git-sync/check` runs before a configuration is trusted, so
    this has to answer without cloning, writing, or pushing anything."""

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
        real_exec = asyncio.create_subprocess_exec

        async def spy(*args, **kwargs):
            commands.append(list(args))
            return await real_exec(*args, **kwargs)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
        monkeypatch.chdir(tmp_path)

        await check_remote_access(_repo_url(origin), "main", token="", timeout=30)

        assert len(commands) == 1
        assert commands[0][1] == "ls-remote"
        assert list(tmp_path.iterdir()) == [origin]
