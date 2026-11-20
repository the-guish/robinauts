# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The branches of a repository as ``GitBranches`` reads them (``orchestrator/adapters/git``),
with the real ``git``, from a repository on disk that has thousands of branches."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from robinauts.orchestrator.adapters.git.branches import GitBranches, git_environment, remote_of
from robinauts.orchestrator.contract.domain import BranchesUnreadableError, Repository
from util.aio import asyncio_test

MANY = 2000
IDENTITY = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def git(*args: str, cwd: Path, given: str | None = None) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        input=given,
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **IDENTITY},
    ).stdout.strip()


class Remote:
    """A bare repository with one commit, which ``branch`` adds a branch to."""

    def __init__(self, path: Path) -> None:
        self.path = path
        git("init", "--bare", "--quiet", str(path), cwd=path.parent)
        tree = git("hash-object", "-t", "tree", "-w", "--stdin", cwd=path, given="")
        self.commit = git("commit-tree", tree, "-m", "first", cwd=path)
        self.repository = Repository("app", f"file://{path}", ("*",))

    def branch(self, *names: str) -> None:
        lines = "".join(f"create refs/heads/{name} {self.commit}\n" for name in names)
        git("update-ref", "--stdin", cwd=self.path, given=lines)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def remote(tmp_path: Path) -> Remote:
    made = Remote(tmp_path / "app.git")
    made.branch("main", *(f"feature/{n:04}" for n in range(MANY)))
    return made


@asyncio_test
async def test_every_branch_is_read_once_and_served_while_fresh(remote: Remote) -> None:
    branches = GitBranches(clock=Clock())

    listed = await branches.listed(remote.repository)
    remote.branch("feature/later")
    again = await branches.listed(remote.repository)

    assert len(listed) == MANY + 1
    assert listed == tuple(sorted(listed))
    assert listed[-1] == "main"
    assert again == listed


@asyncio_test
async def test_a_branch_pushed_since_the_read_exists_at_once(remote: Remote) -> None:
    branches = GitBranches(clock=Clock())
    await branches.listed(remote.repository)
    remote.branch("feature/later")

    assert await branches.exists(remote.repository, "feature/later")
    assert await branches.exists(remote.repository, "feature/0007")
    assert not await branches.exists(remote.repository, "feature/never")
    # A name ls-remote would take as the tail of a longer one is not that branch.
    assert not await branches.exists(remote.repository, "0007")


@asyncio_test
async def test_a_stale_read_serves_once_more_while_a_new_one_runs(remote: Remote) -> None:
    clock = Clock()
    branches = GitBranches(fresh_for=60, clock=clock)
    await branches.listed(remote.repository)
    remote.branch("feature/later")
    clock.now = 61

    stale = await branches.listed(remote.repository)
    for _ in range(200):
        fresh = await branches.listed(remote.repository)
        if "feature/later" in fresh:
            break
        await asyncio.sleep(0.05)

    assert "feature/later" not in stale
    assert "feature/later" in fresh


@asyncio_test
async def test_searches_that_wait_for_the_first_read_share_it(remote: Remote) -> None:
    branches = GitBranches(clock=Clock())

    first, second = await asyncio.gather(
        branches.listed(remote.repository), branches.listed(remote.repository)
    )

    assert first is second


@pytest.mark.parametrize(
    ("url", "git_program", "reason"),
    [
        ("file:///nowhere/app.git", "git", "could not be read: fatal:"),
        ("file:///nowhere/app.git", "/nowhere/git", "could not be read: No such file"),
    ],
)
@asyncio_test
async def test_a_repository_that_cannot_be_read_is_refused_with_why(
    url: str, git_program: str, reason: str
) -> None:
    branches = GitBranches(git=git_program)

    with pytest.raises(BranchesUnreadableError, match=reason) as refused:
        await branches.listed(Repository("app", url, ("*",)))

    assert str(refused.value).startswith("the branches of app ")


@asyncio_test
async def test_a_url_that_would_run_a_command_runs_none(tmp_path: Path) -> None:
    ran = tmp_path / "ran"
    branches = GitBranches()

    with pytest.raises(BranchesUnreadableError):
        await branches.listed(Repository("app", f"ext::sh -c touch% {ran}", ("*",)))

    assert not ran.exists()


def test_git_is_given_the_network_s_settings_and_none_of_the_keys() -> None:
    environ = {
        "PATH": "/usr/bin",
        "HOME": "/home/r",
        "HTTPS_PROXY": "http://proxy:3128",
        "ANTHROPIC_API_KEY": "sk-secret",
        "GIT_DIR": "/elsewhere",
    }

    assert git_environment(environ) == {
        "PATH": "/usr/bin",
        "HOME": "/home/r",
        "HTTPS_PROXY": "http://proxy:3128",
        "GIT_TERMINAL_PROMPT": "0",
    }


@pytest.mark.parametrize(
    ("url", "remote"),
    [
        ("the-guish/fa1", "https://github.com/the-guish/fa1.git"),
        ("the-guish/fa1.git", "https://github.com/the-guish/fa1.git"),
        (" the-guish/fa1 ", "https://github.com/the-guish/fa1.git"),
        ("https://github.com/the-guish/fa1", "https://github.com/the-guish/fa1"),
        ("file:///srv/app.git", "file:///srv/app.git"),
        ("/srv/app.git", "/srv/app.git"),
        ("git@git.example:docs.git", "git@git.example:docs.git"),
        ("-x/app", "-x/app"),
    ],
)
def test_a_repository_named_owner_slash_name_is_read_from_github(url: str, remote: str) -> None:
    assert remote_of(url) == remote
