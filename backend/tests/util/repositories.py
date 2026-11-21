# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Git repositories on disk, for the tests that read a repository's branches with the real
``git``: a bare repository with one commit, and branches added to it at that commit."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

IDENTITY = {
    "GIT_AUTHOR_NAME": "T",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "T",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def _git(path: Path, *args: str, given: str = "") -> str:
    done = subprocess.run(
        ["git", *args],
        cwd=path,
        input=given,
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **IDENTITY},
    )
    return done.stdout.strip()


def bare_repository(path: Path, *branches: str) -> Path:
    """A repository on disk with one commit, and ``branches`` at it."""
    path.mkdir()
    _git(path, "init", "--bare", "--quiet")
    tree = _git(path, "hash-object", "-t", "tree", "-w", "--stdin")
    add_branches(path, _git(path, "commit-tree", tree, "-m", "first"), *branches)
    return path


def add_branches(path: Path, commit: str, *branches: str) -> None:
    """Add ``branches`` to the repository at ``path``, each at ``commit``."""
    lines = "".join(f"create refs/heads/{name} {commit}\n" for name in branches)
    _git(path, "update-ref", "--stdin", given=lines)
