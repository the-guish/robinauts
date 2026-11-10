# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The repositories a worker's agents work on: one clone of each, shared, and a worktree of each
per session, so that agents running at once never touch each other's files.

    <root>/                     the clones, one per repository the flows' settings name
        <repository id>/        on its default branch, fetched before each new worktree
    <session folder>/           the session's own, which an engine gives
        <repository id>/        a worktree of that clone, on the branch rb/<session id>

The repositories are the one list every worker reads: ``[repositories]`` of the flows'
settings, ``flows.toml`` in the folder ``ROBINAUTS_FLOWS_DIR`` names, the same file the server's
flows read (docs/architecture/orchestration.md). Without the variable there is none.

A session's worktrees branch from the clone's default branch, ``origin/HEAD``, whatever its
name, as the repository was last fetched. Each session has a branch of its own, since git checks
a branch out in one worktree at most. The agent may commit there and push: a plain ``git push``
creates ``rb/<session id>`` on GitHub, with the token in the agent's environment
(``credential_helper``), and never writes the token down.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from robinauts.controller.contract.domain import ConfigError
from robinauts.orchestrator.contract.domain import FLOWS_VARIABLE, SETTINGS_FILE
from robinauts.orchestrator.core.config import parse_flows_settings
from robinauts_worker_common.clone import TIMEOUT, GitError, clone, repository_url, run_git
from robinauts_worker_common.github import SERVER, TOKEN_VARIABLE, GitHubError, server_of

BRANCH_PREFIX = "rb/"
"""What a session's branch is named after: ``rb/<session id>``."""

AUTHOR = ("Robinauts", "robinauts@localhost")
"""Who an agent's commits are by, unless the worker's environment says otherwise with git's own
``GIT_AUTHOR_NAME``, ``GIT_AUTHOR_EMAIL``, ``GIT_COMMITTER_NAME`` and ``GIT_COMMITTER_EMAIL``."""

_SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class WorktreeError(GitError):
    """A clone, a fetch or a worktree that failed."""


@dataclass(frozen=True, slots=True)
class Repository:
    """A repository of the flows' settings: its id there, its https URL on the server, and the
    branches a task may start from."""

    id: str
    url: str
    branches: tuple[str, ...] = ()


def repositories_of(
    environ: Mapping[str, str], server: str = SERVER
) -> tuple[tuple[Repository, ...], list[str]]:
    """The repositories of the flows' settings in ``environ``'s flows folder, and what is wrong
    with them, one line each. None, and nothing wrong, without ``ROBINAUTS_FLOWS_DIR`` or
    without a ``flows.toml``."""
    named = environ.get(FLOWS_VARIABLE, "").strip()
    if not named:
        return (), []
    folder = Path(named)
    path = folder / SETTINGS_FILE
    if not folder.is_dir():
        return (), [f"{FLOWS_VARIABLE} names {folder}, which is not a folder in this worker"]
    if not path.is_file():
        return (), []
    try:
        settings = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as refused:
        return (), [f"{path}: {refused}"]
    try:
        flows = parse_flows_settings(settings, folder=folder, default_code=folder)
    except ConfigError as refused:
        # Each line already starts with the file's name.
        return (), [f"{folder}/{line}" for line in str(refused).splitlines()]
    found, problems = [], []
    for repository in flows.repositories:
        try:
            url = repository_url(repository.url, server)
            found.append(Repository(repository.id, url, repository.branches))
        except GitHubError as refused:
            problems.append(f"{path} [repositories.{repository.id}]: {refused}")
        if not _SESSION.fullmatch(repository.id):
            problems.append(
                f"{path} [repositories.{repository.id}]: the id names a folder, so it is made of"
                " letters, digits, dots, dashes and underscores"
            )
    return tuple(found), problems


def repository_problems(environ: Mapping[str, str]) -> list[str]:
    """What is wrong with the repositories this worker would clone, one line each."""
    return repositories_of(environ)[1]


def branch_of(session: str) -> str:
    """The branch of a session's worktrees. ``GitHubError`` for a session id that cannot be part
    of a branch's name and a folder's."""
    if not _SESSION.fullmatch(session) or session.endswith(".lock") or ".." in session:
        raise GitHubError(f"{session!r} cannot name a branch")
    return BRANCH_PREFIX + session


def credential_helper(server: str = SERVER) -> tuple[str, str]:
    """The clone's configuration that lets an agent's own git push with the token in its
    environment: a credential helper for ``server`` alone, which reads ``GITHUB_TOKEN`` when git
    asks and writes it nowhere. The clone's configuration holds the variable's name, never its
    value."""
    script = (
        '!f() { test "$1" = get || exit 0; echo username=x-access-token;'
        f' echo "password=${{{TOKEN_VARIABLE}}}"; }}; f'
    )
    return f"credential.{server_of(server)}/.helper", script


class Repositories:
    """The clones under ``root`` of ``repositories``, and the worktrees of the sessions.

    One process holds a repository's clone at a time: a lock per repository keeps its fetches
    and worktree changes one after the other, while sessions on different repositories go on
    at once."""

    def __init__(
        self,
        root: Path,
        repositories: Sequence[Repository],
        *,
        environ: Mapping[str, str] | None = None,
        server: str = SERVER,
        timeout: float = TIMEOUT,
    ) -> None:
        self.root = root
        self.repositories = tuple(repositories)
        self._environ = environ
        self._server = server
        self._timeout = timeout
        self._locks = {repository.id: asyncio.Lock() for repository in self.repositories}

    @classmethod
    def from_environ(
        cls, root: Path, environ: Mapping[str, str] | None = None
    ) -> Repositories | None:
        """The repositories of the flows' settings ``environ`` names, cloned under ``root``;
        None when there are none. ``ConfigError`` for settings this worker refuses."""
        environ = os.environ if environ is None else environ
        found, problems = repositories_of(environ)
        if problems:
            raise ConfigError("\n".join(problems))
        return cls(root, found, environ=environ) if found else None

    async def _git(self, arguments: Sequence[str], what: str, cwd: Path | None = None) -> str:
        return await run_git(
            arguments,
            environ=self._environ,
            server=self._server,
            cwd=cwd,
            timeout=self._timeout,
            what=what,
            error=WorktreeError,
        )

    def clone_of(self, repository: Repository) -> Path:
        return self.root / repository.id

    async def ensure(self, repository: Repository) -> Path:
        """The repository's clone, made if it is not there yet and fetched otherwise. Takes the
        repository's lock."""
        async with self._locks[repository.id]:
            return await self._ensure(repository)

    async def _ensure(self, repository: Repository) -> Path:
        path = self.clone_of(repository)
        if not (path / ".git").is_dir():
            # A clone that stopped half way leaves a folder git will not clone into.
            shutil.rmtree(path, ignore_errors=True)
            path.parent.mkdir(parents=True, exist_ok=True)
            await clone(
                repository.url,
                path,
                environ=self._environ,
                server=self._server,
                timeout=self._timeout,
            )
            key, helper = credential_helper(self._server)
            name, email = AUTHOR
            for setting in (
                (key, helper),
                ("push.autoSetupRemote", "true"),
                ("user.name", name),
                ("user.email", email),
            ):
                await self._git(["config", *setting], f"configuring {repository.id}", path)
            return path
        await self._git(["fetch", "--prune", "origin"], f"the fetch of {repository.id}", path)
        await self._git(
            ["remote", "set-head", "origin", "--auto"], f"the fetch of {repository.id}", path
        )
        await self._git(["worktree", "prune"], f"pruning {repository.id}", path)
        return path

    async def add_worktrees(
        self,
        session: str,
        folder: Path,
        *,
        starts: Mapping[str, str] | None = None,
        first: bool = True,
    ) -> list[Path]:
        """The session's worktrees in ``folder``, on the branch ``rb/<session>``, made where
        they are missing. A worktree there already is left as it is: it holds the agent's work.
        One whose worktree was lost comes back on its branch, its commits kept.

        A session's repositories are decided at its ``first`` turn: those of ``starts``, each
        from its branch there (a task's, ``tasks.code_task_starts``), or else every repository,
        from its default branch, each as just fetched. A later turn makes none anew, but those
        ``starts`` names. Returns the worktrees the session has."""
        branch = branch_of(session)
        folder.mkdir(parents=True, exist_ok=True)
        unknown = sorted(set(starts or {}) - {repository.id for repository in self.repositories})
        if unknown:
            raise WorktreeError(f"no repository {', '.join(unknown)} to make a worktree of")
        chosen = [r for r in self.repositories if starts is None or r.id in starts]
        made = await asyncio.gather(
            *(
                self._add(
                    repository,
                    branch,
                    folder,
                    start=None if starts is None else starts[repository.id],
                    create=first or starts is not None,
                )
                for repository in chosen
            )
        )
        return [path for path in made if path is not None]

    async def _add(
        self, repository: Repository, branch: str, folder: Path, *, start: str | None, create: bool
    ) -> Path | None:
        target = folder / repository.id
        if (target / ".git").exists():
            return target
        async with self._locks[repository.id]:
            path = await self._ensure(repository)
            known = await self._git(
                ["branch", "--list", branch], f"the worktree of {repository.id}", path
            )
            if known.strip():
                arguments = ["worktree", "add", "--", str(target), branch]
            elif not create:
                return None
            else:
                origin = "origin/HEAD" if start is None else f"origin/{start}"
                arguments = ["worktree", "add", "--no-track", "-b", branch, "--"]
                arguments += [str(target), origin]
            await self._git(arguments, f"the worktree of {repository.id}", path)
        return target

    async def remove_worktrees(self, session: str, folder: Path) -> None:
        """Remove the session's worktrees from ``folder``, and their branch, uncommitted and
        unpushed work with them. Safe to repeat."""
        branch = branch_of(session)
        for repository in self.repositories:
            path = self.clone_of(repository)
            if not (path / ".git").is_dir():
                continue
            async with self._locks[repository.id]:
                target = folder / repository.id
                if (target / ".git").is_file():
                    await self._git(
                        ["worktree", "remove", "--force", "--", str(target)],
                        f"removing the worktree of {repository.id}",
                        path,
                    )
                await self._git(["worktree", "prune"], f"pruning {repository.id}", path)
                if (await self._git(["branch", "--list", branch], "listing", path)).strip():
                    await self._git(
                        ["branch", "-D", branch], f"removing the branch of {repository.id}", path
                    )
