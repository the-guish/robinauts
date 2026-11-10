# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Clone a GitHub repository inside the worker's container, with the worker's GitHub token when
it was handed one (github.py).

``await clone("owner/name", path)`` from an engine or a tool, or from a shell in the container:

    python -m robinauts_worker_common.clone owner/name /workspace/name [--branch B] [--depth N]

git runs with an environment of this module's own making: the token reaches it as configuration
in its environment, never on its command line, where any process could read it, and never in
the clone's ``.git/config``, where it would outlive the clone. git asks no one for a password,
reads no configuration but this, and speaks https alone, submodules included.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import urlsplit

from robinauts_worker_common.github import (
    SERVER,
    TOKEN_VARIABLE,
    GitHubError,
    authorization,
    git_config,
    server_of,
    token_of,
)

TIMEOUT = 600.0
"""Seconds a clone may take before it is stopped."""

_OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
_NAME = r"[A-Za-z0-9._-]+"
_SHORT = re.compile(rf"({_OWNER})/({_NAME})")
_PATH = re.compile(rf"/({_OWNER})/({_NAME})/?")

PASSED_ON = (
    "PATH",
    "HOME",
    "LANG",
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
    "GIT_SSL_CAINFO",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)
"""What git is handed of the worker's environment: where programs are, the home directory, and
how this network reaches the server. Nothing else, the worker's other secrets least of all."""


class CloneError(Exception):
    """A clone that was refused or failed, said in a few lines that never hold the token."""


def repository_url(source: str, server: str = SERVER) -> str:
    """The https URL of the repository ``source`` names on ``server``: ``owner/name``, or that
    repository's URL on that server, with or without ``.git``. ``GitHubError`` for anything
    else: another host, another scheme, credentials in the URL, a path that is not a
    repository's."""
    base = server_of(server)
    text = source.strip()
    found = _SHORT.fullmatch(text)
    if found is None:
        parts = urlsplit(text)
        if parts.username or parts.password:
            raise GitHubError(
                f"a repository URL may not carry credentials: the token goes in {TOKEN_VARIABLE}"
            )
        if f"{parts.scheme}://{parts.netloc}".lower() != base:
            raise GitHubError(f"{source!r} is not a repository on {base}")
        found = None if parts.query or parts.fragment else _PATH.fullmatch(parts.path)
        if found is None:
            raise GitHubError(f"{source!r} is not owner/name, nor a repository's URL on {base}")
    owner, name = found.groups()
    name = name.removesuffix(".git")
    if not name or name in (".", ".."):
        raise GitHubError(f"{source!r} names no repository")
    return f"{base}/{owner}/{name}.git"


def clone_command(
    url: str, destination: Path, branch: str | None = None, depth: int | None = None
) -> list[str]:
    """git's command line: options in their ``--name=value`` form, so that no value is read as
    an option, and the URL and the destination after ``--``."""
    command = ["git", "clone", "--quiet"]
    if branch is not None:
        if not branch.strip():
            raise GitHubError("the branch to clone is empty")
        command.append(f"--branch={branch}")
    if depth is not None:
        if depth < 1:
            raise GitHubError(f"the depth of a clone is 1 or more, not {depth}")
        command.append(f"--depth={depth}")
    return [*command, "--", url, str(destination)]


def git_environment(
    environ: Mapping[str, str], token: str | None, server: str = SERVER
) -> dict[str, str]:
    """The whole environment git runs with: what ``PASSED_ON`` names of ``environ``, and the
    configuration of ``git_config``, given as variables git reads and writes nowhere."""
    environment = {name: environ[name] for name in PASSED_ON if name in environ}
    environment.setdefault("PATH", os.defpath)
    environment.update(
        GIT_TERMINAL_PROMPT="0",
        GIT_ASKPASS="",
        SSH_ASKPASS="",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_ALLOW_PROTOCOL="https",
    )
    config = git_config(token, server)
    environment["GIT_CONFIG_COUNT"] = str(len(config))
    for index, (key, value) in enumerate(config):
        environment[f"GIT_CONFIG_KEY_{index}"] = key
        environment[f"GIT_CONFIG_VALUE_{index}"] = value
    return environment


REFUSED = ("could not read Username", "Authentication failed", "Invalid username or token")
"""What git says when the server refused the header: with prompts off, it asks for a user name
it is not allowed to ask for."""


def failure(stderr: str, token: str | None) -> str:
    """What git said, as a ``CloneError`` says it: its last lines, with the token and its header
    taken out, and a word on the token: that there was none, or that it was refused."""
    if token is not None:
        stderr = stderr.replace(authorization(token), "***").replace(token, "***")
    lines = [line for line in stderr.strip().splitlines() if line.strip()][-5:]
    said = "\n".join(lines) or "git said nothing"
    if token is None:
        said += f"\n(no {TOKEN_VARIABLE} was handed to this worker: a private repository needs one)"
    elif any(sign in stderr for sign in REFUSED):
        said += (
            f"\n(the server refused the token in {TOKEN_VARIABLE}: it may have expired, been"
            " revoked, or not reach this repository. Make a new one on GitHub and replace the"
            " value; Robinauts never makes one)"
        )
    return f"the clone failed:\n{said}"


async def clone(
    source: str,
    destination: str | os.PathLike[str],
    *,
    branch: str | None = None,
    depth: int | None = None,
    environ: Mapping[str, str] | None = None,
    server: str = SERVER,
    timeout: float = TIMEOUT,
) -> Path:
    """Clone the repository ``source`` names (``repository_url``) into ``destination``, which
    must not exist or be an empty directory, with the token in ``environ`` (the process's own
    when left out). Returns the destination.

    ``CloneError`` for a clone that fails or takes longer than ``timeout`` seconds, and
    ``GitHubError`` for one refused before git runs. git is stopped when the clone is cancelled,
    and the cancellation goes on."""
    environ = os.environ if environ is None else environ
    url = repository_url(source, server)
    target = Path(destination)
    command = clone_command(url, target, branch, depth)
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise GitHubError(f"{target} already holds something: a clone goes into a new directory")
    token = token_of(environ)
    process = await asyncio.create_subprocess_exec(
        *command,
        env=git_environment(environ, token, server),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(timeout):
            _, stderr = await process.communicate()
    except TimeoutError:
        raise CloneError(f"the clone of {url} took longer than {timeout:g} seconds") from None
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode != 0:
        raise CloneError(failure(stderr.decode(errors="replace"), token))
    return target


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m robinauts_worker_common.clone",
        description=f"Clone a GitHub repository, with {TOKEN_VARIABLE} when it is set.",
    )
    parser.add_argument("source", help="owner/name, or the repository's https URL")
    parser.add_argument("destination", help="a directory that does not exist yet, or is empty")
    parser.add_argument("--branch", help="the branch or tag to check out")
    parser.add_argument("--depth", type=int, help="this many commits of history, no more")
    parser.add_argument("--server", default=SERVER, help=f"the GitHub server ({SERVER})")
    parser.add_argument("--timeout", type=float, default=TIMEOUT, help="seconds, at most")
    arguments = parser.parse_args(argv)
    try:
        path = asyncio.run(
            clone(
                arguments.source,
                arguments.destination,
                branch=arguments.branch,
                depth=arguments.depth,
                server=arguments.server,
                timeout=arguments.timeout,
            )
        )
    except (GitHubError, CloneError) as refused:
        print(refused, file=sys.stderr)
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
