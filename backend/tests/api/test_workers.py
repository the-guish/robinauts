# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""An agent that names a worker has its turns run by that worker's process alone; the others,
by the default worker's. The server spawns one process per worker, and its broker hands each
the turns of its own agents: with one of them paused, its agents' turns wait for it, and the
other worker's still run."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import asyncpg
import httpx
import pytest

from util import stack
from util.controller_db import requires_postgres
from util.fake_openai import FakeLocalGPTServer

pytestmark = [
    *requires_postgres,
    pytest.mark.skipif(sys.platform != "linux", reason="reads the processes from /proc"),
]

SPECIAL = """
[agents.special_one]
title = "Special one"
system_prompt = "{system_prompt}"
models = ["local_gpt"]
engine = "pydantic-ai"
worker = "special"
"""


def workers_of(server: int) -> dict[str, int]:
    """The worker processes ``server`` spawned, by the worker each runs."""
    found = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            parent = int((entry / "stat").read_text().rsplit(")", 1)[1].split()[1])
            environ = (entry / "environ").read_bytes().split(b"\0")
        except (OSError, IndexError, ValueError):
            continue
        if parent != server:
            continue
        for variable in environ:
            if variable.startswith(b"ROBINAUTS_WORKER_NAME="):
                found[variable.partition(b"=")[2].decode()] = int(entry.name)
    return found


def task_of(database_url: str, turn: str) -> tuple[str, str]:
    """The worker and the state of the task of that turn."""

    async def read() -> tuple[str, str]:
        connection = await asyncpg.connect(database_url)
        try:
            row = await connection.fetchrow(
                "SELECT k.worker_queue, k.state FROM turns AS t JOIN tasks AS k ON k.id = t.task_id"
                " WHERE t.id = $1::uuid",
                turn,
            )
        finally:
            await connection.close()
        assert row is not None
        return row["worker_queue"], row["state"]

    return asyncio.run(read())


def queued(database_url: str) -> list[str]:
    """The worker of each queued task."""

    async def read() -> list[str]:
        connection = await asyncpg.connect(database_url)
        try:
            rows = await connection.fetch("SELECT worker_queue FROM tasks WHERE state = 'queued'")
        finally:
            await connection.close()
        return [row["worker_queue"] for row in rows]

    return asyncio.run(read())


def wait_until_queued(database_url: str) -> list[str]:
    deadline = time.monotonic() + 30
    while not (found := queued(database_url)):
        assert time.monotonic() < deadline, "no task was ever queued"
        time.sleep(0.1)
    return found


def test_each_worker_runs_the_turns_of_its_own_agents(
    local_gpt: FakeLocalGPTServer, tmp_path: Path
) -> None:
    config = tmp_path / "robinauts.toml"
    config.write_text(
        stack.config_for(local_gpt.base_url) + SPECIAL.format(system_prompt=stack.SYSTEM_PROMPT)
    )
    started: list[subprocess.Popen[bytes]] = []
    with (
        stack.database(config) as url,
        stack.server(config, url, env=stack.API_KEY, started=started) as server,
        httpx.Client(base_url=server, timeout=30.0) as api,
    ):
        workers = workers_of(started[0].pid)
        assert sorted(workers) == ["default", "special"]

        runs = {}
        for agent in ("langchain", "pydantic_ai", "special_one"):
            done = api.post("/api/turns", json={"agent_id": agent, "text": "hello"})
            assert '"RUN_FINISHED"' in done.text, done.text
            runs[agent] = done.headers["x-robinauts-run-id"]
        assert {agent: task_of(url, run) for agent, run in runs.items()} == {
            "langchain": ("default", "done"),
            "pydantic_ai": ("default", "done"),
            "special_one": ("special", "done"),
        }

        # The special worker paused: its agent's turn waits for it, unclaimed, while the
        # default worker runs its own; once it goes on, it runs the turn that waited.
        os.kill(workers["special"], signal.SIGSTOP)
        pool = ThreadPoolExecutor(1)
        try:
            later = pool.submit(
                api.post, "/api/turns", json={"agent_id": "special_one", "text": "later"}
            )
            assert wait_until_queued(url) == ["special"]
            now = api.post("/api/turns", json={"agent_id": "pydantic_ai", "text": "now"})
            assert '"RUN_FINISHED"' in now.text, now.text
            assert queued(url) == ["special"]
        finally:
            os.kill(workers["special"], signal.SIGCONT)
            pool.shutdown(wait=True)
        answered = later.result()
        assert '"RUN_FINISHED"' in answered.text, answered.text
        assert task_of(url, answered.headers["x-robinauts-run-id"]) == ("special", "done")
