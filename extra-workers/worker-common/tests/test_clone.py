# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A clone, by the real git, from the local stand-in for the GitHub server
(``github_stand_in``). What it checks: the token reaches the server in the header and nowhere
else, git asks no one for a password, and a refusal never repeats the token.

Skipped where git or openssl is missing.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from robinauts_worker_common.clone import CloneError, clone, main
from robinauts_worker_common.github import GitHubError, authorization
from tests.conftest import NEEDS_GIT
from tests.github_stand_in import Server, repository

pytestmark = NEEDS_GIT

TOKEN = "github_pat_11ABCDEFG_made0by0the0person"


@pytest.fixture(scope="module", autouse=True)
def hello(stand_in: Server) -> None:
    repository(stand_in, "octo/hello")


def environ(server: Server, **more: str) -> dict[str, str]:
    return server.environ(**more)


def run(server: Server, destination: Path, **options) -> Path:
    environment = options.pop("environ", None) or environ(server)
    return asyncio.run(
        clone("octo/hello", destination, server=server.url, environ=environment, **options)
    )


def test_a_public_repository_is_cloned_without_a_token(server: Server, tmp_path: Path) -> None:
    target = run(server, tmp_path / "hello")
    assert (target / "README.md").read_text() == "hello\n"
    assert server.seen
    assert set(server.seen) == {None}


def test_the_token_goes_in_the_header_and_nowhere_else(server: Server, tmp_path: Path) -> None:
    server.token = TOKEN
    target = run(server, tmp_path / "hello", environ=environ(server, GITHUB_TOKEN=TOKEN))
    assert (target / "README.md").exists()
    assert set(server.seen) == {authorization(TOKEN)}
    written = b"".join(path.read_bytes() for path in (target / ".git").rglob("*") if path.is_file())
    assert TOKEN.encode() not in written
    assert authorization(TOKEN).encode() not in written


def test_a_private_repository_without_a_token_fails_at_once(server: Server, tmp_path: Path) -> None:
    server.token = TOKEN
    with pytest.raises(CloneError) as refused:
        run(server, tmp_path / "hello", timeout=30)
    assert "no GITHUB_TOKEN was handed to this worker" in str(refused.value)
    assert not (tmp_path / "hello").exists()


def test_a_refused_token_is_not_repeated(server: Server, tmp_path: Path) -> None:
    server.token = "github_pat_another"
    with pytest.raises(CloneError) as refused:
        run(server, tmp_path / "hello", environ=environ(server, GITHUB_TOKEN=TOKEN), timeout=30)
    assert TOKEN not in str(refused.value)
    assert "the server refused the token in GITHUB_TOKEN" in str(refused.value)


def test_a_branch_and_a_depth(server: Server, tmp_path: Path) -> None:
    target = run(server, tmp_path / "hello", branch="other", depth=1)
    head = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=target, capture_output=True, text=True
    )
    assert head.stdout.strip() == "other"
    assert (target / ".git" / "shallow").exists()


def test_a_destination_that_holds_something_is_refused(server: Server, tmp_path: Path) -> None:
    (tmp_path / "hello").mkdir()
    (tmp_path / "hello" / "mine").write_text("x")
    with pytest.raises(GitHubError, match="already holds something"):
        run(server, tmp_path / "hello")
    assert server.seen == []
    (tmp_path / "empty").mkdir()
    assert (run(server, tmp_path / "empty") / "README.md").exists()


def test_the_command_line(
    server: Server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setenv("GIT_SSL_CAINFO", str(server.certificate))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    target = tmp_path / "hello"
    assert main(["octo/hello", str(target), "--server", server.url]) == 0
    assert capsys.readouterr().out.strip() == str(target)
    assert main(["gitlab.com/octo/hello", str(tmp_path / "x"), "--server", server.url]) == 1
    assert "is not a repository on" in capsys.readouterr().err
