# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""An external worker on a real server: the server starts and is healthy with nobody connected
as that worker; a turn of its agent waits, queued for it, while the default worker runs its
own; and the worker, reaching ``/internal/worker/`` with the secret the configuration's
variable holds, claims and ends it. Any other credential is refused."""

from __future__ import annotations

import asyncio
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import httpx

from robinauts.shared_domain.domain import TurnState
from robinauts.worker.http_turn_broker import HttpTurnBroker
from util import stack
from util.controller_db import requires_postgres
from util.fake_openai import FakeLocalGPTServer

pytestmark = requires_postgres

WORKER = "claude-container"
VARIABLE = "ROBINAUTS_CLAUDE_CONTAINER_SECRET"
SECRET = "the-secret-the-container-presents"

EXTERNAL = """
[workers.claude-container]
kind = "external"
secret_env = "ROBINAUTS_CLAUDE_CONTAINER_SECRET"

[agents.claude-container]
title = "Claude (container)"
system_prompt = "{system_prompt}"
model = "local_gpt"
engine = "claude-code"
worker = "claude-container"
"""


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


async def claim_and_end(server: str, secret: str) -> uuid.UUID:
    """What the external worker does, through the worker's own client: claim its one task and
    end the turn it names, as failed, since no engine runs here."""
    broker = HttpTurnBroker(server, secret)
    try:
        now = datetime.now(UTC)
        tasks = await broker.claim_tasks(now, now + timedelta(seconds=90), 10)
        assert len(tasks) == 1
        assert tasks[0].worker == WORKER
        found = await broker.find_turn(uuid.UUID(tasks[0].payload["turn_id"]))
        assert found is not None
        owner, turn = found
        await broker.finish_turn(
            owner,
            turn.session_id,
            turn.id,
            TurnState.FAILED,
            now,
            "ended by the test",
            None,
            [],
            now,
        )
        return turn.id
    finally:
        await broker.close()


def test_an_external_worker_claims_its_agents_turns_over_the_api(
    local_gpt: FakeLocalGPTServer, tmp_path: Path
) -> None:
    config = tmp_path / "robinauts.toml"
    config.write_text(
        stack.config_for(local_gpt.base_url) + EXTERNAL.format(system_prompt=stack.SYSTEM_PROMPT)
    )
    started: list[subprocess.Popen[bytes]] = []
    environment = {**stack.API_KEY, VARIABLE: SECRET}
    with (
        stack.database(config) as url,
        stack.server(config, url, env=environment, started=started) as server,
        httpx.Client(base_url=server, timeout=30.0) as api,
    ):
        # Up, and healthy, with nobody connected as the external worker.
        assert api.get("/health").json() == {"status": "ok"}

        pool = ThreadPoolExecutor(1)
        try:
            later = pool.submit(api.post, "/api/turns", json={"agent_id": WORKER, "text": "hello"})
            assert wait_until_queued(url) == [WORKER]
            now = api.post("/api/turns", json={"agent_id": "pydantic_ai", "text": "now"})
            assert '"RUN_FINISHED"' in now.text, now.text
            assert queued(url) == [WORKER]
            ended = asyncio.run(claim_and_end(server, SECRET))
            answered = later.result()
        finally:
            pool.shutdown(wait=True)
        assert answered.headers["x-robinauts-run-id"] == str(ended)
        assert '"RUN_ERROR"' in answered.text, answered.text
        assert task_of(url, str(ended)) == (WORKER, "done")

        refused = api.post(
            "/internal/worker/tasks/claim",
            json={
                "now": datetime.now(UTC).isoformat(),
                "until": (datetime.now(UTC) + timedelta(seconds=90)).isoformat(),
                "limit": 1,
            },
            headers={"Authorization": "Bearer not-the-secret"},
        )
        assert refused.status_code == 401
