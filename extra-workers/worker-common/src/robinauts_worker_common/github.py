# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The GitHub token a worker may be handed, and how git is given it.

Robinauts creates, requests, stores, prints and shares no credential (extra-workers/README.md).
The person or the operator makes a token on GitHub and puts it in a file of the worker's own,
which the worker's command names with ``docker run --env-file`` (README.md). This module reads
the *name* of the variable, checks that what it holds can be a token, and turns it into the one
header git sends to the GitHub server, and nowhere else. The value is never logged, never
written to disk and never put on a command line. An agent whose own commands run in a
subprocess that inherits the worker's environment, as Claude Code's ``Bash`` does, can read it
there: that is allowed (README.md, "Enhancements").
"""

from __future__ import annotations

import base64
import re
from collections.abc import Mapping
from urllib.parse import urlsplit

TOKEN_VARIABLE = "GITHUB_TOKEN"
"""A GitHub token: a fine-grained personal access token, or an app's installation token. It is
optional: without it, public repositories alone can be cloned."""

SERVER = "https://github.com"
"""The GitHub server a token is sent to, and the only one a repository is cloned from. A GitHub
Enterprise Server is named in its place, the same way: ``https://`` and a host."""

_TOKEN = re.compile(r"[A-Za-z0-9_]+")
"""What every kind of GitHub token is made of: letters, digits and underscores, no more."""


class GitHubError(ValueError):
    """A token, a server or a repository this worker refuses, said in one line that never
    holds the token."""


def token_problems(environ: Mapping[str, str]) -> list[str]:
    """What is wrong with the token in ``environ``, one line each; none when it is well formed
    or not there at all. The lines name the variable and never repeat its value."""
    if TOKEN_VARIABLE not in environ:
        return []
    value = environ[TOKEN_VARIABLE]
    if not value.strip():
        return [
            f"{TOKEN_VARIABLE} is set and empty: put the token you made on GitHub in it, or"
            " leave the variable out to clone public repositories alone"
        ]
    if not _TOKEN.fullmatch(value):
        return [
            f"{TOKEN_VARIABLE} holds what a GitHub token never has: a space, a quote or a line"
            " break. `docker run --env-file` keeps quotes as they are: write NAME=value, with"
            " no quotes around the value"
        ]
    return []


def token_of(environ: Mapping[str, str]) -> str | None:
    """The token in ``environ``, or None when it is not there. ``GitHubError`` for one that is
    there and is not well formed."""
    problems = token_problems(environ)
    if problems:
        raise GitHubError(problems[0])
    return environ.get(TOKEN_VARIABLE)


def server_of(server: str) -> str:
    """``server`` as git config keys and repository URLs use it: ``https://host[:port]``, with
    no path, no credentials, no query. ``GitHubError`` for anything else."""
    parts = urlsplit(server.strip())
    if parts.scheme != "https" or not parts.hostname:
        raise GitHubError(f"the GitHub server must be https://<host>, not {server!r}")
    if parts.username or parts.password:
        raise GitHubError("the GitHub server's URL may not carry credentials")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise GitHubError(f"the GitHub server must be https://<host> alone, not {server!r}")
    return f"https://{parts.netloc.lower()}"


def authorization(token: str) -> str:
    """The header value git sends with a token: basic authentication, GitHub's own user name
    for tokens, and the token as the password."""
    pair = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
    return f"Basic {pair}"


def git_config(token: str | None, server: str = SERVER) -> list[tuple[str, str]]:
    """The git configuration a clone runs with, as (key, value) pairs: no credential helper of
    anyone else's, and, with a token, the header that carries it, scoped to ``server`` so that
    no other host is ever sent it."""
    config = [("credential.helper", "")]
    if token is not None:
        config.append(
            (f"http.{server_of(server)}/.extraHeader", f"Authorization: {authorization(token)}")
        )
    return config
