# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The whole platform for ``tests/api`` and ``tests/e2e``: a real ``robinauts`` server on a
database of its own in PostgreSQL, configured with one agent per engine on models that
``FakeLocalGPTServer`` serves. The server runs in oneberth's local mode: the test holds its
launch secret, and ``signed_in`` signs the local user in with a ticket, as the window does.

Given an MCP server, the configuration also has one agent per engine with that server's tools,
``<engine>_tools``. The database is made in the PostgreSQL that ``ROBINAUTS_TEST_DATABASE_URL``
names and dropped afterwards.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
from oneberth.identity import CredentialKind, new_secret

from robinauts.cli.lifecycle import WORKER_START_SECONDS

ROOT = Path(__file__).resolve().parents[3]
ROBINAUTS = Path(sys.executable).with_name("robinauts")

AGENTS = ("langchain", "pydantic_ai")
"""The agents of the configuration, one per engine, without tools."""

SYSTEM_PROMPT = "You are a robinaut."

CONFIG = """
[model_providers.local_gpt]
kind = "openai-compatible"
base_url = "{base_url}"
api_key_env = "LOCAL_GPT_KEY"

[models.local_gpt]
provider = "local_gpt"
name = "fake-gpt"
title = "Local GPT"

[models.local_gpt_2]
provider = "local_gpt"
name = "fake-gpt-2"
title = "Local GPT 2"

[agents.langchain]
title = "LangChain"
system_prompt = "{system_prompt}"
models = ["local_gpt", "local_gpt_2"]
engine = "langchain"

[agents.pydantic_ai]
title = "Pydantic AI"
system_prompt = "{system_prompt}"
models = ["local_gpt", "local_gpt_2"]
engine = "pydantic-ai"

[agents.pinned]
title = "Pinned to GPT 2"
system_prompt = "{system_prompt}"
models = ["local_gpt_2"]
engine = "pydantic-ai"
"""

TOOLS_CONFIG = """
[tool_servers.tools]
url = "{tools_url}"
auth = "none"

[agents.langchain_tools]
title = "LangChain with tools"
system_prompt = "{system_prompt}"
models = ["local_gpt"]
engine = "langchain"
tools = ["tools"]

[agents.pydantic_ai_tools]
title = "Pydantic AI with tools"
system_prompt = "{system_prompt}"
models = ["local_gpt"]
engine = "pydantic-ai"
tools = ["tools"]
"""

API_KEY = {"LOCAL_GPT_KEY": "not-a-real-key"}

STOP_SECONDS = 10
"""How long a stop may take. A long poll, Robinauts' or neorc's, ends when the backend stops."""

HEALTHY = {"status": "ok", "components": {"robinauts": "ok", "neorc": "ok"}}
"""What ``/health`` answers while every worker is heard from."""


def config_for(base_url: str, tools_url: str | None = None) -> str:
    config = CONFIG.format(base_url=base_url, system_prompt=SYSTEM_PROMPT)
    if tools_url is not None:
        config += TOOLS_CONFIG.format(tools_url=tools_url, system_prompt=SYSTEM_PROMPT)
    return config


def sent(request: dict[str, Any]) -> list[tuple[str, str]]:
    """The conversation a request carried, as ``(role, text)``, its system prompt checked."""
    system, *rest = request["messages"]
    assert (system["role"], system["content"]) == ("system", SYSTEM_PROMPT)
    return [(m["role"], m["content"]) for m in rest]


class Server(str):
    """A running server's URL, and the launch secret it was started with."""

    launch_secret: str
    worker_url: str

    def __new__(cls, url: str, launch_secret: str, worker_url: str = "") -> Server:
        made = super().__new__(cls, url)
        made.launch_secret = launch_secret
        made.worker_url = worker_url
        return made


def ticket_url(server: Server) -> str:
    """Where a new ticket signs the local user in, asked for with the launch secret."""
    request = urllib.request.Request(
        server + "/auth/tickets",
        data=b"{}",
        headers={
            "authorization": f"Bearer {server.launch_secret}",
            "content-type": "application/json",
        },
    )
    with urllib.request.urlopen(request) as answer:
        return str(json.load(answer)["url"])


def signed_in(client: httpx.Client, server: Server) -> httpx.Client:
    """``client``, with the local user's session cookie, and the ``Origin`` a browser sends."""
    opened = client.get(ticket_url(server))
    assert opened.status_code == 303, opened.text
    client.headers["origin"] = server
    return client


@contextmanager
def client(server: Server, timeout: float = 30.0) -> Iterator[httpx.Client]:
    """A client of ``server``, the local user signed in."""
    with httpx.Client(base_url=server, timeout=timeout) as made:
        yield signed_in(made, server)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_until_up(url: str, server: subprocess.Popen[bytes]) -> None:
    # A server listens once its slowest worker has started, and the server gives each one
    # WORKER_START_SECONDS: each worker imports every installed engine first.
    deadline = time.monotonic() + WORKER_START_SECONDS
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise RuntimeError(f"the server exited with {server.returncode}")
        try:
            with urllib.request.urlopen(url + "/auth/session"):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"nothing answered on {url}")


def wait_until_ready(server: subprocess.Popen[bytes]) -> None:
    """Read the server's standard output until it says ``ready``: once both listeners serve
    and neorc has started. The rest is read and dropped as it comes, so that the server never
    waits on a full pipe: its access log is on its standard output too."""
    assert server.stdout is not None
    for line in server.stdout:
        if line.strip() == b"ready":
            threading.Thread(target=server.stdout.read, daemon=True).start()
            return
    raise RuntimeError(f"the server ended before it was ready, with {server.wait()}")


async def run_statement(url: str, statement: str) -> None:
    connection = await asyncpg.connect(url)
    try:
        await connection.execute(statement)
    finally:
        await connection.close()


@contextmanager
def new_database(prefix: str = "robinauts_stack_") -> Iterator[str]:
    """The URL of a new, empty database on the PostgreSQL ``ROBINAUTS_TEST_DATABASE_URL``
    names, dropped afterwards with whatever is still connected to it.

    A database rather than a schema: oneberth keeps its store in a schema named ``oneberth``
    and neorc in one named ``neorc``, whatever the ``search_path``, and neorc's notifications
    go on channels of fixed names. Two servers on one database would share those -- a local
    start ends every session in ``oneberth.sessions``, its own and the other server's alike.
    A database of its own keeps a server's every table and channel apart from the rest, so
    that servers can run side by side, as ``pytest-xdist``'s workers run them."""
    given = os.environ.get("ROBINAUTS_TEST_DATABASE_URL")
    if not given:
        pytest.fail("no ROBINAUTS_TEST_DATABASE_URL: set it to a PostgreSQL these tests may use")
    name = prefix + uuid.uuid4().hex
    asyncio.run(run_statement(given, f'CREATE DATABASE "{name}"'))
    try:
        yield str(httpx.URL(given).copy_with(path=f"/{name}"))
    finally:
        asyncio.run(run_statement(given, f'DROP DATABASE "{name}" WITH (FORCE)'))


@contextmanager
def database(config: Path) -> Iterator[str]:
    """The URL of a new database (``new_database``), with this build's tables and oneberth's
    store in it, dropped afterwards."""
    with new_database() as url:
        environment = {**os.environ, "ROBINAUTS_CONFIG": str(config)}
        environment["ROBINAUTS_DATABASE_URL"] = url
        subprocess.run([str(ROBINAUTS), "db", "init"], env=environment, check=True)
        subprocess.run([str(ROBINAUTS), "store", "init"], env=environment, check=True)
        yield url


def launched(
    command: list[str], environment: Mapping[str, str], launch_secret: str, **popen: Any
) -> subprocess.Popen[bytes]:
    """The server's process, its launch secret on its standard input as a launcher writes it."""
    process = subprocess.Popen(command, env=environment, stdin=subprocess.PIPE, **popen)
    assert process.stdin is not None
    process.stdin.write(f"{launch_secret}\n".encode())
    process.stdin.close()
    # communicate() flushes the standard input it finds, and Python 3.12 refuses to flush a
    # closed file: the process has none any more.
    process.stdin = None
    return process


def start_local(
    environment: Mapping[str, str], command: list[str] | None = None, **popen: Any
) -> tuple[subprocess.Popen[bytes], Server]:
    """A server in the local mode, started with ``command`` in the place of ``robinauts``, and
    its URL with its launch secret. The caller waits for it and stops it."""
    port, worker_port = free_port(), free_port()
    arguments = [
        "start",
        "--local",
        "--port",
        str(port),
        "--worker-port",
        str(worker_port),
    ]
    secret = new_secret(CredentialKind.LAUNCH)
    process = launched([*(command or [str(ROBINAUTS)]), *arguments], environment, secret, **popen)
    url = Server(f"http://127.0.0.1:{port}", secret, f"http://127.0.0.1:{worker_port}")
    return process, url


@contextmanager
def server(
    config: Path,
    database_url: str,
    env: Mapping[str, str] = {},
    started: list[subprocess.Popen[bytes]] | None = None,
) -> Iterator[Server]:
    """A server on that config, in the local mode, stopped afterwards. Its process is added to
    ``started``, for a test that kills it."""
    port, worker_port = free_port(), free_port()
    environment = {**os.environ, **env, "ROBINAUTS_CONFIG": str(config)}
    environment["ROBINAUTS_DATABASE_URL"] = database_url
    command = [str(ROBINAUTS), "start", "--local", "--port", str(port)]
    command += ["--worker-port", str(worker_port)]
    secret = new_secret(CredentialKind.LAUNCH)
    with launched(command, environment, secret) as running:
        if started is not None:
            started.append(running)
        url = Server(f"http://127.0.0.1:{port}", secret, f"http://127.0.0.1:{worker_port}")
        try:
            wait_until_up(url, running)
            yield url
        finally:
            running.terminate()
            running.wait(timeout=STOP_SECONDS)
