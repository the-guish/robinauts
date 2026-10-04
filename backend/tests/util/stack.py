# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The whole platform for ``tests/api`` and ``tests/e2e``: a real ``robinauts`` server on a
schema of its own in PostgreSQL, configured with one agent per engine on models that
``FakeLocalGPTServer`` serves.

Given an MCP server, the configuration also has one agent per engine with that server's tools,
``<engine>_tools``. A turn may make ``MAX_MODEL_CALLS_PER_TURN`` model calls, fewer than the
default, so that a test can reach the bound quickly. The schema is made in the PostgreSQL that
``ROBINAUTS_TEST_DATABASE_URL`` names and dropped afterwards.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import asyncpg
import pytest

ROOT = Path(__file__).resolve().parents[3]
ROBINAUTS = Path(sys.executable).with_name("robinauts")

AGENTS = ("langchain", "pydantic_ai")
"""The agents of the configuration, one per engine, without tools."""

SYSTEM_PROMPT = "You are a robinaut."

MAX_MODEL_CALLS_PER_TURN = 60

CONFIG = """
max_model_calls_per_turn = {max_model_calls_per_turn}

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
model = "local_gpt"
engine = "langchain"

[agents.pydantic_ai]
title = "Pydantic AI"
system_prompt = "{system_prompt}"
model = "local_gpt"
engine = "pydantic-ai"
"""

TOOLS_CONFIG = """
[tool_servers.tools]
url = "{tools_url}"
auth = "none"

[agents.langchain_tools]
title = "LangChain with tools"
system_prompt = "{system_prompt}"
model = "local_gpt"
engine = "langchain"
tools = ["tools"]

[agents.pydantic_ai_tools]
title = "Pydantic AI with tools"
system_prompt = "{system_prompt}"
model = "local_gpt"
engine = "pydantic-ai"
tools = ["tools"]
"""

API_KEY = {"LOCAL_GPT_KEY": "not-a-real-key"}


def config_for(base_url: str, tools_url: str | None = None) -> str:
    config = CONFIG.format(
        base_url=base_url,
        system_prompt=SYSTEM_PROMPT,
        max_model_calls_per_turn=MAX_MODEL_CALLS_PER_TURN,
    )
    if tools_url is not None:
        config += TOOLS_CONFIG.format(tools_url=tools_url, system_prompt=SYSTEM_PROMPT)
    return config


def sent(request: dict[str, Any]) -> list[tuple[str, str]]:
    """The conversation a request carried, as ``(role, text)``, its system prompt checked."""
    system, *rest = request["messages"]
    assert (system["role"], system["content"]) == ("system", SYSTEM_PROMPT)
    return [(m["role"], m["content"]) for m in rest]


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_until_up(url: str, server: subprocess.Popen[bytes]) -> None:
    for _ in range(100):
        if server.poll() is not None:
            raise RuntimeError(f"the server exited with {server.returncode}")
        try:
            with urllib.request.urlopen(url + "/auth/session"):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"nothing answered on {url}")


def with_search_path(url: str, schema: str) -> str:
    """``url`` with every connection made from it working in ``schema``."""
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query) + [("search_path", schema)]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


async def run_statement(url: str, statement: str) -> None:
    connection = await asyncpg.connect(url)
    try:
        await connection.execute(statement)
    finally:
        await connection.close()


@contextmanager
def database(config: Path) -> Iterator[str]:
    """The URL of a new schema, with this build's tables in it, dropped afterwards."""
    given = os.environ.get("ROBINAUTS_TEST_DATABASE_URL")
    if not given:
        pytest.fail("no ROBINAUTS_TEST_DATABASE_URL: set it to a PostgreSQL these tests may use")
    schema = "robinauts_stack_" + uuid.uuid4().hex
    asyncio.run(run_statement(given, f'CREATE SCHEMA "{schema}"'))
    try:
        url = with_search_path(given, schema)
        environment = {**os.environ, "ROBINAUTS_CONFIG": str(config)}
        environment["ROBINAUTS_DATABASE_URL"] = url
        subprocess.run([str(ROBINAUTS), "db", "init"], env=environment, check=True)
        yield url
    finally:
        asyncio.run(run_statement(given, f'DROP SCHEMA "{schema}" CASCADE'))


@contextmanager
def server(config: Path, database_url: str, env: Mapping[str, str] = {}) -> Iterator[str]:
    """The URL of a server on that config, without sign-in, stopped afterwards."""
    port = free_port()
    environment = {**os.environ, **env, "ROBINAUTS_CONFIG": str(config)}
    environment["ROBINAUTS_DATABASE_URL"] = database_url
    command = [str(ROBINAUTS), "start", "--dev-no-sign-in", "--port", str(port)]
    with subprocess.Popen(command, env=environment) as running:
        url = f"http://127.0.0.1:{port}"
        try:
            wait_until_up(url, running)
            yield url
        finally:
            running.terminate()
            running.wait(timeout=10)
