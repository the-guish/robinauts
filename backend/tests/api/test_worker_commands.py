# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A worker whose ``[workers.<name>]`` table names a command is started with that command in
place of this build's worker module, and differs in nothing else: it reads its secret on its
standard input, says ``ready`` on its standard output, runs the turns of its own agents, and
``/health`` hears it through its requests to the broker API. Here the command is a shell that
runs the worker module, which stands for a container: what crosses an ``exec`` is what crosses
``docker run -i``, the standard streams, the signal and the variables passed on. It runs in a
process of its own on PostgreSQL, where every worker does, and in the server's process alone
too, where the rest run in-process.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import asyncpg
import httpx
import pytest

from util import stack
from util.controller_db import DATABASE_URL, NO_DATABASE
from util.fake_openai import FakeLocalGPTServer

SIX_MINUTES = 360

SPECIAL = """
[agents.special_one]
title = "Special one"
system_prompt = "{system_prompt}"
models = ["local_gpt"]
engine = "pydantic-ai"
worker = "special"

[workers.special]
command = ["sh", "-c", "exec \\"$@\\"", "sh", "{python}", "-m", "robinauts.cli.worker_process"]
"""

ECHO = """
[model_providers.echo]
kind = "anthropic"
api_key_env = "ECHO_API_KEY"

[models.echo]
provider = "echo"
name = "echo"
title = "Echo"

[agents.echo]
title = "Echo"
system_prompt = ""
models = ["echo"]
engine = "echo"
worker = "special"

[workers.special]
command = ["sh", "-c", "exec \\"$@\\"", "sh", "{python}", "-m", "robinauts.cli.worker_process"]
"""

CLOSED = b"the controller is closed"


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


@pytest.mark.io
@pytest.mark.database
@pytest.mark.skipif(DATABASE_URL is None, reason=NO_DATABASE)
def test_a_worker_started_with_a_command_runs_its_turns_and_is_heard_through_the_api(
    local_gpt: FakeLocalGPTServer, tmp_path: Path
) -> None:
    config = tmp_path / "robinauts.toml"
    special = SPECIAL.format(system_prompt=stack.SYSTEM_PROMPT, python=sys.executable)
    config.write_text(stack.config_for(local_gpt.base_url) + special)
    started: list[subprocess.Popen[bytes]] = []
    with (
        stack.database(config) as url,
        stack.server(config, url, env=stack.API_KEY, started=started) as server,
        httpx.Client(base_url=server, timeout=30.0) as api,
    ):
        done = api.post("/api/turns", json={"agent_id": "special_one", "text": "hello"})
        assert '"RUN_FINISHED"' in done.text, done.text
        assert task_of(url, done.headers["x-robinauts-run-id"]) == ("special", "done")

        # The server hears the worker through the API: not touched for six minutes, its file
        # says it is gone; its next requests, those of a turn, say it is back.
        activity = Path(tempfile.gettempdir()) / f"robinauts-worker-{started[0].pid}-special.alive"
        assert api.get("/health").json() == {"status": "ok"}
        six_minutes_ago = time.time() - SIX_MINUTES
        os.utime(activity, (six_minutes_ago, six_minutes_ago))
        assert api.get("/health").status_code == 503
        again = api.post("/api/turns", json={"agent_id": "special_one", "text": "again"})
        assert '"RUN_FINISHED"' in again.text, again.text
        assert api.get("/health").status_code == 200


@pytest.mark.io
def test_a_worker_with_a_command_runs_in_a_process_of_its_own_beside_the_in_process_ones(
    tmp_path: Path,
) -> None:
    config = tmp_path / "robinauts.toml"
    config.write_text(ECHO.format(python=sys.executable))
    environment = {**os.environ, "ROBINAUTS_CONFIG": str(config)}
    environment.pop("ROBINAUTS_DATABASE_URL", None)
    port = stack.free_port()
    command = [str(stack.ROBINAUTS), "start", "--dev-no-sign-in", "--port", str(port)]
    with subprocess.Popen(command, env=environment, stderr=subprocess.PIPE) as running:
        try:
            url = f"http://127.0.0.1:{port}"
            stack.wait_until_up(url, running)
            here = Path(tempfile.gettempdir())
            files = [
                here / f"robinauts-worker-{running.pid}.alive",
                here / f"robinauts-worker-{running.pid}-special.alive",
            ]
            with httpx.Client(base_url=url, timeout=30.0) as api:
                assert api.get("/health").json() == {"status": "ok"}
                done = api.post("/api/turns", json={"agent_id": "echo", "text": "hello"})
                assert '"RUN_FINISHED"' in done.text, done.text
                assert '"delta":"The tool said: "' in done.text, done.text
            assert all(file.exists() for file in files)
            running.send_signal(signal.SIGTERM)
            _, said = running.communicate(timeout=30)
        finally:
            # A failed assertion must not leave a server the `with` then waits for forever.
            if running.poll() is None:
                running.kill()
    assert CLOSED in said, said.decode(errors="replace")
    assert b"worker 'special' starts with the command: sh -c" in said, said.decode()
    assert not any(file.exists() for file in files)
