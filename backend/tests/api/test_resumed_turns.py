# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A server killed in the middle of a turn: the next server ends the turn as interrupted, and
leaves it so. Asked to resume it, it goes on from the rounds its engine saved, in the engines
that keep their memory in PostgreSQL."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest

from util import stack
from util.controller_db import requires_postgres
from util.fake_openai import CallTool, FakeLocalGPTServer
from util.mcp_server import mcp_server

pytestmark = requires_postgres

ROUNDS = 3

FAST_LEASES = """
[work]
lease_seconds = 2
heartbeat_seconds = 1
sweep_seconds = 1
"""

COUNTER = """
[tool_servers.counter]
url = "{tools_url}"
auth = "none"

[agents.langchain_counter]
title = "LangChain, counting"
system_prompt = "{system_prompt}"
model = "local_gpt"
engine = "langchain"
tools = ["counter"]

[agents.pydantic_ai_counter]
title = "Pydantic AI, counting"
system_prompt = "{system_prompt}"
model = "local_gpt"
engine = "pydantic-ai"
tools = ["counter"]
"""

counted: list[int] = []
released = threading.Event()


async def count(n: int) -> str:
    """Counts ``n``; the last count waits until the test lets it go."""
    counted.append(n)
    if n == ROUNDS - 1:
        await asyncio.to_thread(released.wait, 30)
    return f"counted {n}"


class CountingModel:
    """Calls ``count`` three times, one round each, then answers "done"."""

    def reply(self, messages: list[dict[str, Any]]) -> str | CallTool:
        rounds = sum(m["role"] == "tool" for m in messages)
        if rounds < ROUNDS:
            return CallTool("count", {"n": rounds}, call_id=f"call_{rounds}")
        return "done"


@pytest.fixture(scope="module")
def counter_url() -> Iterator[str]:
    with mcp_server(count) as url:
        yield url


def children(pid: int) -> list[int]:
    with open(f"/proc/{pid}/task/{pid}/children") as listed:
        return [int(child) for child in listed.read().split()]


def kill_with_its_worker(server: subprocess.Popen[bytes]) -> None:
    """SIGKILL for the server's worker, then for the server: a crash, which ends nothing."""
    for worker in children(server.pid):
        os.kill(worker, signal.SIGKILL)
    server.kill()
    server.wait()


def start_turn(server: str, agent: str) -> None:
    """Ask, and read the stream until the server dies."""
    try:
        with httpx.Client(base_url=server, timeout=60.0) as api:
            api.post("/api/turns", json={"agent_id": agent, "text": "go"})
    except httpx.HTTPError:
        pass


def opened(api: httpx.Client) -> dict[str, Any]:
    """The only conversation, opened."""
    [conversation] = api.get("/api/conversations").json()["items"]
    return api.get(f"/api/conversations/{conversation['id']}").json()


def wait_for(condition: Any, seconds: float = 30.0) -> Any:
    for _ in range(int(seconds * 10)):
        if found := condition():
            return found
        time.sleep(0.1)
    raise AssertionError("not in time")


# Not kataloop: its memory is in the worker's process until `kataloop[postgres]`, so the server
# that resumes the turn has no session to resume it from.
@pytest.mark.parametrize("agent", ["langchain_counter", "pydantic_ai_counter"])
def test_a_crashed_turn_waits_until_asked_to_resume(
    model_server: FakeLocalGPTServer, counter_url: str, tmp_path: Path, agent: str
) -> None:
    model_server.model = CountingModel()
    counted.clear()
    released.clear()
    config = tmp_path / "robinauts.toml"
    counter = COUNTER.format(tools_url=counter_url, system_prompt=stack.SYSTEM_PROMPT)
    config.write_text(stack.config_for(model_server.base_url) + counter + FAST_LEASES)
    started: list[subprocess.Popen[bytes]] = []
    with stack.database(config) as url, ThreadPoolExecutor(1) as thread:
        with stack.server(config, url, stack.API_KEY, started) as first:
            asking = thread.submit(start_turn, first, agent)
            wait_for(lambda: len(counted) == ROUNDS)
            kill_with_its_worker(started[0])
        asking.result(timeout=30)
        released.set()

        with (
            stack.server(config, url, stack.API_KEY) as second,
            httpx.Client(base_url=second, timeout=60.0) as api,
        ):
            # Its lease passes, and a reader ends it; nothing runs it again.
            ended = wait_for(lambda: (o := opened(api))["ended_badly"] and o)
            assert ended["ended_badly"]["state"] == "interrupted"
            time.sleep(3)
            assert opened(api)["run_id"] is None
            assert counted == [0, 1, 2]

            failed = ended["messages"][-1]
            assert failed["failed"]
            cid = ended["conversation"]["id"]
            question = ended["messages"][0]["id"]
            refused = api.post(f"/api/conversations/{cid}/turns", json={"resume": question})
            assert refused.status_code == 404
            resumed = api.post(f"/api/conversations/{cid}/turns", json={"resume": failed["id"]})
            assert resumed.status_code == 200, resumed.text
            assert '"RUN_FINISHED"' in resumed.text
            answer = opened(api)["messages"][-1]

    assert [part["text"] for part in answer["parts"] if part["kind"] == "text"] == ["done"]
    # The rounds it had saved are not run again; the call cut short is.
    assert counted == [0, 1, 2, 2]
