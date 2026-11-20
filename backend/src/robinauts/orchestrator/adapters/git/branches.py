# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The branches of a repository, as ``git ls-remote`` reads them from its server. A read serves
for a short while; after it, the next search is answered from it while a new read runs. A
branch the last read did not have is asked of the server alone, so a branch pushed a moment ago
starts a run."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from robinauts.orchestrator.contract.domain import BranchesUnreadableError, Repository
from robinauts.orchestrator.ports.branches import BranchNames

_log = logging.getLogger(__name__)

HEADS = "refs/heads/"

PASSED_ON = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "GIT_SSL_CAINFO",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)
"""What git is given of this server's environment: where it runs, and how it reaches the
network. The keys of the configuration stay behind."""

REASON_LIMIT = 300
"""The most of git's last line a refusal repeats."""


@dataclass(frozen=True, slots=True)
class _Read:
    at: float
    names: tuple[str, ...]


def git_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """git's environment: what ``PASSED_ON`` names of ``environ``, and no prompt for a
    password, which no one would answer."""
    kept = {name: environ[name] for name in PASSED_ON if name in environ}
    return {**kept, "GIT_TERMINAL_PROMPT": "0"}


class GitBranches(BranchNames):
    """The branches of each repository, read with ``git`` and kept ``fresh_for`` seconds. A
    read that takes over ``timeout`` seconds is ended, and refused."""

    def __init__(
        self,
        *,
        fresh_for: float = 60.0,
        timeout: float = 20.0,
        git: str = "git",
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fresh_for = fresh_for
        self._timeout = timeout
        self._git = git
        self._environ = git_environment(os.environ if environ is None else environ)
        self._clock = clock
        self._reads: dict[str, _Read] = {}
        self._reading: dict[str, asyncio.Task[tuple[str, ...]]] = {}

    async def listed(self, repository: Repository) -> tuple[str, ...]:
        kept = self._reads.get(repository.url)
        if kept is None:
            return await self._read(repository)
        if self._clock() - kept.at >= self._fresh_for:
            # Answered from the last read; the next search has the new one.
            self._reading_task(repository)
        return kept.names

    async def exists(self, repository: Repository, branch: str) -> bool:
        kept = self._reads.get(repository.url)
        if kept is not None and branch in kept.names:
            return True
        return branch in await self._ls_remote(repository, f"{HEADS}{branch}")

    async def _read(self, repository: Repository) -> tuple[str, ...]:
        """Every branch, as one read shared by every search that waits for it."""
        return await asyncio.shield(self._reading_task(repository))

    def _reading_task(self, repository: Repository) -> asyncio.Task[tuple[str, ...]]:
        task = self._reading.get(repository.url)
        if task is None:
            task = asyncio.create_task(self._read_and_keep(repository))
            task.add_done_callback(lambda done: _said(repository, done))
            self._reading[repository.url] = task
        return task

    async def _read_and_keep(self, repository: Repository) -> tuple[str, ...]:
        try:
            names = await self._ls_remote(repository)
            self._reads[repository.url] = _Read(at=self._clock(), names=names)
            return names
        finally:
            self._reading.pop(repository.url, None)

    async def _ls_remote(self, repository: Repository, *patterns: str) -> tuple[str, ...]:
        """The branches of ``repository`` that match ``patterns``, or all of them, sorted."""
        # `ext::` would run a command the URL names; the settings' URL stays a URL.
        command = (
            self._git,
            "-c",
            "protocol.ext.allow=never",
            "ls-remote",
            "--heads",
            "--refs",
            "--",
            repository.url,
            *patterns,
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._environ,
            )
        except OSError as error:
            raise BranchesUnreadableError(
                f"the branches of {repository.id} could not be read: {error.strerror}"
            ) from None
        try:
            out, err = await asyncio.wait_for(process.communicate(), self._timeout)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            raise BranchesUnreadableError(
                f"the branches of {repository.id} could not be read: its server did not answer"
                f" within {self._timeout:g} s"
            ) from None
        if process.returncode != 0:
            reason = reason_of(err.decode(errors="replace"), process.returncode)
            raise BranchesUnreadableError(
                f"the branches of {repository.id} could not be read: {reason}"
            )
        return heads_of(out.decode(errors="replace"))


def reason_of(said: str, code: int | None) -> str:
    """Why git failed, as its first ``fatal:`` or ``error:`` line says, or its last line:
    git explains a refusal over several lines, and the first of those says what it was."""
    lines = [line.strip() for line in said.splitlines() if line.strip()]
    first = next((line for line in lines if line.startswith(("fatal:", "error:"))), None)
    reason = first or (lines[-1] if lines else f"git ended with {code}")
    return reason[:REASON_LIMIT]


def heads_of(listing: str) -> tuple[str, ...]:
    """The branch names of ``git ls-remote --heads`` output, sorted."""
    names = []
    for line in listing.splitlines():
        _, _, ref = line.partition("\t")
        if ref.startswith(HEADS):
            names.append(ref.removeprefix(HEADS))
    return tuple(sorted(names))


def _said(repository: Repository, done: asyncio.Task[tuple[str, ...]]) -> None:
    """A read that failed is logged, whether a search waited for it or it ran in the
    background, where the last read keeps serving."""
    if not done.cancelled() and (error := done.exception()) is not None:
        _log.warning("%s", error)
