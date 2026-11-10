# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The shared clones and the sessions' worktrees: which repositories the flows' settings name,
read without git; then, by the real git against the local stand-in for GitHub
(``github_stand_in``), that sessions at once each get worktrees of their own on ``rb/<id>``,
from the default branch as just fetched, that an agent's own git pushes with the token in its
environment, and that a session's worktrees and branch go with it."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from robinauts_worker_common.__main__ import problems_of
from robinauts_worker_common.github import GitHubError, authorization
from robinauts_worker_common.worktrees import (
    Repositories,
    Repository,
    WorktreeError,
    branch_of,
    credential_helper,
    repositories_of,
)
from tests.conftest import NEEDS_GIT
from tests.github_stand_in import QUIET_GIT, Server, commit, git, repository

TOKEN = "github_pat_11ABCDEFG_made0by0the0person"
SESSION = "6f1c2a7e-0d4b-4f7e-9a51-3c2b1d0e9f8a"


@pytest.mark.parametrize(
    ("session", "branch"),
    [
        (SESSION, f"rb/{SESSION}"),
        ("abc_1.2", "rb/abc_1.2"),
        ("", None),
        ("-x", None),
        ("../x", None),
        ("a..b", None),
        ("a b", None),
        ("a/b", None),
        ("x.lock", None),
    ],
)
def test_a_session_has_a_branch_of_its_own(session, branch) -> None:
    if branch is None:
        with pytest.raises(GitHubError):
            branch_of(session)
    else:
        assert branch_of(session) == branch


def test_the_agents_git_reads_the_token_from_its_environment() -> None:
    key, script = credential_helper("https://GitHub.com/")
    assert key == "credential.https://github.com/.helper"
    assert '"password=${GITHUB_TOKEN}"' in script
    assert script.startswith('!f() { test "$1" = get || exit 0;')


FLOWS = {
    "two": """
[repositories.robinauts]
url = "https://github.com/the-guish/robinauts"
branches = ["main"]

[repositories.neorc]
url = "https://github.com/the-guish/neorc.git"
branches = ["main", "dev"]
""",
    "elsewhere": """
[repositories.lab]
url = "https://gitlab.com/octo/lab"
branches = ["main"]
""",
    "spaced": """
[repositories."a b"]
url = "https://github.com/octo/ab"
branches = ["main"]
""",
    "unknown": """
[repositories.x]
url = "https://github.com/octo/x"
branch = "main"
""",
    "broken": "[repositories.x\n",
}


@pytest.mark.parametrize(
    ("flows", "found", "said"),
    [
        (None, [], []),
        ("two", ["robinauts", "neorc"], []),
        ("elsewhere", [], ["[repositories.lab]: 'https://gitlab.com/octo/lab' is not"]),
        ("spaced", ["a b"], ["[repositories.a b]: the id names a folder"]),
        (
            "unknown",
            [],
            ["/flows.toml [repositories].x branch: unknown key", "x branches: a list of"],
        ),
        ("broken", [], ["flows.toml: "]),
    ],
)
def test_the_repositories_are_the_flows_settings(tmp_path: Path, flows, found, said) -> None:
    if flows is not None:
        (tmp_path / "flows.toml").write_text(FLOWS[flows])
    environ = {"ROBINAUTS_FLOWS_DIR": str(tmp_path)}
    repositories, problems = repositories_of(environ)
    assert [repository.id for repository in repositories] == found
    assert len(problems) == len(said)
    for problem, start in zip(problems, said, strict=True):
        assert start in problem
    assert problems_of(environ) == problems
    if flows == "two":
        assert [repository.url for repository in repositories] == [
            "https://github.com/the-guish/robinauts.git",
            "https://github.com/the-guish/neorc.git",
        ]


def test_without_a_flows_folder_there_are_none(tmp_path: Path) -> None:
    assert repositories_of({}) == ((), [])
    assert Repositories.from_environ(tmp_path, {}) is None
    (problem,) = repositories_of({"ROBINAUTS_FLOWS_DIR": str(tmp_path / "none")})[1]
    assert problem.endswith("which is not a folder in this worker")


# With git, against the stand-in.


def _name() -> str:
    return f"octo/r{uuid.uuid4().hex[:8]}"


def repositories(server: Server, root: Path, *names: str, **environ: str) -> Repositories:
    found = [Repository(name.split("/")[1], f"{server.url}/{name}.git") for name in names]
    return Repositories(root, found, environ=server.environ(**environ), server=server.url)


def head(path: Path, ref: str = "HEAD") -> str:
    return git("rev-parse", ref, cwd=path).strip()


@NEEDS_GIT
def test_sessions_at_once_get_worktrees_of_their_own(server: Server, tmp_path: Path) -> None:
    one, two = _name(), _name()
    bare_one, _ = repository(server, one), repository(server, two)
    repos = repositories(server, tmp_path / "repos", one, two)
    first, second = tmp_path / "s1", tmp_path / "s2"

    async def both() -> list[list[Path]]:
        return await asyncio.gather(
            repos.add_worktrees("s1", first), repos.add_worktrees("s2", second)
        )

    made = asyncio.run(both())
    names = [one.split("/")[1], two.split("/")[1]]
    assert made == [[first / name for name in names], [second / name for name in names]]
    for session, folder in (("s1", first), ("s2", second)):
        for name in names:
            worktree = folder / name
            assert (worktree / ".git").is_file()
            assert (worktree / "README.md").read_text() == "hello\n"
            assert git("branch", "--show-current", cwd=worktree).strip() == f"rb/{session}"
    assert head(first / names[0]) == head(bare_one, "main")
    assert (tmp_path / "repos" / names[0] / ".git").is_dir()
    # The agent's work stays: a second call leaves a worktree there as it is.
    (first / names[0] / "mine.txt").write_text("work")
    asyncio.run(repos.add_worktrees("s1", first))
    assert (first / names[0] / "mine.txt").read_text() == "work"


@NEEDS_GIT
def test_a_new_worktree_starts_from_the_default_branch_as_just_fetched(
    server: Server, tmp_path: Path
) -> None:
    name = _name()
    repository(server, name, branch="master")
    repos = repositories(server, tmp_path / "repos", name)
    short = name.split("/")[1]
    asyncio.run(repos.add_worktrees("old", tmp_path / "old"))
    commit(server, name, "later.txt", "later\n")
    asyncio.run(repos.add_worktrees("new", tmp_path / "new"))
    assert (tmp_path / "new" / short / "later.txt").exists()
    assert not (tmp_path / "old" / short / "later.txt").exists()
    assert head(tmp_path / "new" / short) == head(server.root / f"{name}.git", "master")


@NEEDS_GIT
def test_a_task_s_worktrees_are_of_its_repositories_from_their_branches(
    server: Server, tmp_path: Path
) -> None:
    """A code task's first turn: the repositories it names, each from its branch; the others
    get none, at that turn or a later one."""
    named, other = _name(), _name()
    repository(server, named)
    repository(server, other)
    commit(server, named, "later.txt", "later\n")  # main moves on; the branch other does not
    repos = repositories(server, tmp_path / "repos", named, other)
    short = named.split("/")[1]
    session = tmp_path / "task"
    made = asyncio.run(repos.add_worktrees("task", session, starts={short: "other"}))
    assert made == [session / short]
    assert head(session / short) == head(server.root / f"{named}.git", "other")
    assert not (session / short / "later.txt").exists()
    assert git("branch", "--show-current", cwd=session / short).strip() == "rb/task"
    # A later turn keeps what the first decided, and makes none of the others.
    again = asyncio.run(repos.add_worktrees("task", session, first=False))
    assert again == [session / short]
    assert not (session / other.split("/")[1]).exists()


@NEEDS_GIT
def test_a_later_turn_brings_a_lost_worktree_back_and_makes_no_new_one(
    server: Server, tmp_path: Path
) -> None:
    name = _name()
    repository(server, name)
    repos = repositories(server, tmp_path / "repos", name)
    assert asyncio.run(repos.add_worktrees("late", tmp_path / "late", first=False)) == []
    (worktree,) = asyncio.run(repos.add_worktrees("s", tmp_path / "s"))
    shutil.rmtree(worktree)
    assert asyncio.run(repos.add_worktrees("s", tmp_path / "s", first=False)) == [worktree]


def test_a_task_naming_a_repository_this_worker_has_not_is_refused(tmp_path: Path) -> None:
    repos = Repositories(tmp_path / "repos", [Repository("app", "https://github.com/o/a.git")])
    with pytest.raises(WorktreeError, match="no repository web to make a worktree of"):
        asyncio.run(repos.add_worktrees("s", tmp_path / "s", starts={"web": "main"}))


@NEEDS_GIT
def test_the_agents_git_pushes_its_branch_with_the_token(server: Server, tmp_path: Path) -> None:
    name = _name()
    bare = repository(server, name)
    server.token = TOKEN
    repos = repositories(server, tmp_path / "repos", name, GITHUB_TOKEN=TOKEN)
    (worktree,) = asyncio.run(repos.add_worktrees(SESSION, tmp_path / "session"))
    (worktree / "change.txt").write_text("done\n")
    # The agent's own git, in the worker's environment: the token in GITHUB_TOKEN, nothing else.
    agent = {
        **os.environ,
        **QUIET_GIT,
        "GITHUB_TOKEN": TOKEN,
        "GIT_SSL_CAINFO": str(server.certificate),
        "GIT_TERMINAL_PROMPT": "0",
    }
    for command in (["add", "change.txt"], ["commit", "-qm", "change"], ["push", "-q"]):
        subprocess.run(["git", *command], cwd=worktree, env=agent, check=True, capture_output=True)
    assert head(bare, f"rb/{SESSION}") == head(worktree)
    assert authorization(TOKEN) in server.seen
    configs = [tmp_path / "repos" / name.split("/")[1] / ".git" / "config", worktree / ".git"]
    assert not any(TOKEN in path.read_text() for path in configs)


@NEEDS_GIT
def test_a_lost_worktree_comes_back_on_its_branch(server: Server, tmp_path: Path) -> None:
    name = _name()
    repository(server, name)
    repos = repositories(server, tmp_path / "repos", name)
    (worktree,) = asyncio.run(repos.add_worktrees("s", tmp_path / "s"))
    (worktree / "kept.txt").write_text("kept\n")
    git("add", "kept.txt", cwd=worktree)
    git("commit", "-qm", "kept", cwd=worktree)
    shutil.rmtree(worktree)
    (again,) = asyncio.run(repos.add_worktrees("s", tmp_path / "s"))
    assert (again / "kept.txt").read_text() == "kept\n"


@NEEDS_GIT
def test_a_sessions_worktrees_and_branch_go_with_it(server: Server, tmp_path: Path) -> None:
    name = _name()
    repository(server, name)
    repos = repositories(server, tmp_path / "repos", name)
    clone = tmp_path / "repos" / name.split("/")[1]
    (worktree,) = asyncio.run(repos.add_worktrees("s", tmp_path / "s"))
    (worktree / "unsaved.txt").write_text("x")
    asyncio.run(repos.remove_worktrees("s", tmp_path / "s"))
    assert not worktree.exists()
    assert git("branch", "--list", "rb/s", cwd=clone).strip() == ""
    asyncio.run(repos.remove_worktrees("s", tmp_path / "s"))
    asyncio.run(repos.remove_worktrees("never", tmp_path / "never"))
