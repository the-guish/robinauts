# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The GitHub token, the server, and the repository a clone may name: pure functions, checked
without git or the network."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from robinauts_worker_common.__main__ import problems_of
from robinauts_worker_common.clone import (
    clone_command,
    failure,
    git_environment,
    repository_url,
)
from robinauts_worker_common.github import (
    GitHubError,
    authorization,
    git_config,
    server_of,
    token_of,
    token_problems,
)

TOKEN = "github_pat_11ABCDEFG_made0by0the0person"


@pytest.mark.parametrize(
    ("environ", "said"),
    [
        ({}, None),
        ({"GITHUB_TOKEN": TOKEN}, None),
        ({"GITHUB_TOKEN": "ghp_" + "a" * 36}, None),
        ({"GITHUB_TOKEN": ""}, "GITHUB_TOKEN is set and empty"),
        ({"GITHUB_TOKEN": "  "}, "GITHUB_TOKEN is set and empty"),
        ({"GITHUB_TOKEN": f'"{TOKEN}"'}, "GITHUB_TOKEN holds what a GitHub token never has"),
        ({"GITHUB_TOKEN": f"{TOKEN}\n"}, "GITHUB_TOKEN holds what a GitHub token never has"),
        ({"GITHUB_TOKEN": f"{TOKEN} x"}, "GITHUB_TOKEN holds what a GitHub token never has"),
    ],
)
def test_the_token_is_optional_and_well_formed(environ, said) -> None:
    problems = token_problems(environ)
    assert problems_of(environ) == problems
    if said is None:
        assert problems == []
        assert token_of(environ) == environ.get("GITHUB_TOKEN")
        return
    (problem,) = problems
    assert problem.startswith(said)
    assert TOKEN not in problem
    with pytest.raises(GitHubError, match=said):
        token_of(environ)


@pytest.mark.parametrize(
    ("server", "expected"),
    [
        ("https://github.com", "https://github.com"),
        ("https://GitHub.com/", "https://github.com"),
        ("https://ghe.example.com:8443", "https://ghe.example.com:8443"),
        ("http://github.com", None),
        ("github.com", None),
        ("https://user:pass@github.com", None),
        ("https://github.com/org", None),
        ("https://github.com?x=1", None),
    ],
)
def test_the_server_is_https_and_a_host_alone(server, expected) -> None:
    if expected is None:
        with pytest.raises(GitHubError):
            server_of(server)
    else:
        assert server_of(server) == expected


def test_the_token_goes_in_one_header_scoped_to_the_server() -> None:
    pair = base64.b64decode(authorization(TOKEN).removeprefix("Basic ")).decode()
    assert pair == f"x-access-token:{TOKEN}"
    assert git_config(None) == [("credential.helper", "")]
    assert git_config(TOKEN, "https://ghe.example.com/") == [
        ("credential.helper", ""),
        ("http.https://ghe.example.com/.extraHeader", f"Authorization: {authorization(TOKEN)}"),
    ]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("octo/hello", "https://github.com/octo/hello.git"),
        ("octo/hello.git", "https://github.com/octo/hello.git"),
        ("octo-cat/hello.world_2", "https://github.com/octo-cat/hello.world_2.git"),
        ("https://github.com/octo/hello", "https://github.com/octo/hello.git"),
        ("https://github.com/octo/hello.git", "https://github.com/octo/hello.git"),
        ("https://github.com/octo/hello/", "https://github.com/octo/hello.git"),
        (" octo/hello ", "https://github.com/octo/hello.git"),
        ("octo/hello/tree/main", None),
        ("https://github.com/octo/hello/tree/main", None),
        ("https://github.com/octo/hello?x=1", None),
        ("https://gitlab.com/octo/hello", None),
        ("http://github.com/octo/hello", None),
        ("git@github.com:octo/hello.git", None),
        ("ssh://git@github.com/octo/hello.git", None),
        ("file:///srv/octo/hello", None),
        ("ext::sh -c touch% /tmp/x", None),
        (f"https://x-access-token:{TOKEN}@github.com/octo/hello", None),
        ("-u/hello", None),
        ("octo/.git", None),
        ("octo/..", None),
    ],
)
def test_a_repository_is_on_the_server_by_https(source, expected) -> None:
    if expected is None:
        with pytest.raises(GitHubError) as refused:
            repository_url(source)
        assert TOKEN not in str(refused.value)
    else:
        assert repository_url(source) == expected


def test_a_repository_on_another_server() -> None:
    url = repository_url("https://ghe.example.com/octo/hello", "https://ghe.example.com")
    assert url == "https://ghe.example.com/octo/hello.git"
    with pytest.raises(GitHubError):
        repository_url("https://github.com/octo/hello", "https://ghe.example.com")


@pytest.mark.parametrize(
    ("branch", "depth", "options"),
    [
        (None, None, []),
        ("main", None, ["--branch=main"]),
        ("-x", 1, ["--branch=-x", "--depth=1"]),
        ("", None, None),
        (None, 0, None),
    ],
)
def test_no_value_is_read_as_an_option(branch, depth, options) -> None:
    url, target = "https://github.com/octo/hello.git", Path("/w/hello")
    if options is None:
        with pytest.raises(GitHubError):
            clone_command(url, target, branch, depth)
        return
    assert clone_command(url, target, branch, depth) == [
        "git",
        "clone",
        "--quiet",
        *options,
        "--",
        url,
        "/w/hello",
    ]


def test_git_is_handed_its_own_environment() -> None:
    environ = {
        "PATH": "/usr/bin",
        "HOME": "/var/lib/robinauts",
        "HTTPS_PROXY": "http://proxy:3128",
        "GITHUB_TOKEN": TOKEN,
        "ANTHROPIC_API_KEY": "k",
        "GIT_CONFIG_PARAMETERS": "'core.sshCommand'='touch /tmp/x'",
    }
    environment = git_environment(environ, TOKEN)
    assert environment["PATH"] == "/usr/bin"
    assert environment["HTTPS_PROXY"] == "http://proxy:3128"
    for absent in ("GITHUB_TOKEN", "ANTHROPIC_API_KEY", "GIT_CONFIG_PARAMETERS"):
        assert absent not in environment
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["GIT_ALLOW_PROTOCOL"] == "https"
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_CONFIG_COUNT"] == "2"
    assert environment["GIT_CONFIG_KEY_1"] == "http.https://github.com/.extraHeader"
    assert environment["GIT_CONFIG_VALUE_1"] == f"Authorization: {authorization(TOKEN)}"
    without = git_environment({}, None)
    assert without["GIT_CONFIG_COUNT"] == "1"
    assert "GIT_CONFIG_KEY_1" not in without
    assert without["PATH"]


USERNAME = "fatal: could not read Username for 'https://github.com': terminal prompts disabled\n"


@pytest.mark.parametrize(
    ("stderr", "token", "said", "hint"),
    [
        ("fatal: repository not found\n", None, "fatal: repository not found", "none"),
        (USERNAME, None, "could not read Username", "none"),
        (USERNAME, TOKEN, "could not read Username", "refused"),
        ("remote: Invalid username or token.\n", TOKEN, "Invalid username", "refused"),
        (f"fatal: {TOKEN} was refused\n", TOKEN, "fatal: *** was refused", None),
        (f"x {authorization(TOKEN)}\n", TOKEN, "x ***", None),
        ("", TOKEN, "git said nothing", None),
    ],
)
def test_a_failure_never_repeats_the_token(stderr, token, said, hint) -> None:
    message = failure(stderr, token)
    assert said in message
    assert TOKEN not in message
    assert ("a private repository needs one" in message) is (hint == "none")
    assert ("the server refused the token" in message) is (hint == "refused")
