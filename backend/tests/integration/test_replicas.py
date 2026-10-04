# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Two processes of the deployment on one database, as two pods behind a load balancer.

Each test starts two real ``robinauts start`` processes, on the echo agent, the local
development mode and one schema of its own, with a short lease. A turn is started on one
and watched, stopped or found through the other; one is killed, or stopped, in the middle
of a turn. The echo engine's ``slowly`` prompt answers a word a second, which is what makes a
turn long enough to do that to.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import httpx

from aio import asyncio_test
from controller_db import requires_postgres, temporary_schema
from robinauts.controller.composition import init_database, load

pytestmark = requires_postgres

EXAMPLE = Path(__file__).resolve().parents[3] / "examples" / "echo.toml"
WORK = """
[work]
lease_seconds = 3
heartbeat_seconds = 1
drain_seconds = 2
"""
SLOW = "slowly " + " ".join(f"w{n}" for n in range(30))
"""A turn of thirty seconds and more."""

START = "import sys; from robinauts.web.cli import run; run(sys.argv[1:])"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Pod:
    def __init__(self, name: str, process: subprocess.Popen[bytes], url: str, log: Path) -> None:
        self.name = name
        self.process = process
        self.url = url
        self.log = log

    def said(self) -> str:
        return self.log.read_text(encoding="utf-8", errors="replace")


@contextmanager
def started(name: str, config: Path, dsn: str, folder: Path) -> Iterator[Pod]:
    port = free_port()
    log = folder / f"{name}.log"
    environment = {
        **os.environ,
        "ROBINAUTS_CONFIG": str(config),
        "ROBINAUTS_DATABASE_URL": dsn,
        "ROBINAUTS_WORKER_ID": name,
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    command = [sys.executable, "-c", START, "start", "--dev-no-sign-in", "--port", str(port)]
    with log.open("wb") as out:
        process = subprocess.Popen(command, env=environment, stdout=out, stderr=subprocess.STDOUT)
    pod = Pod(name, process, f"http://127.0.0.1:{port}", log)
    try:
        for _ in range(200):
            if process.poll() is not None:
                raise RuntimeError(f"{name} exited with {process.returncode}:\n{pod.said()}")
            try:
                if httpx.get(pod.url + "/ready", timeout=1.0).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError(f"{name} never became ready:\n{pod.said()}")
        yield pod
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=30)


@asynccontextmanager
async def two_pods(folder: Path) -> AsyncIterator[tuple[Pod, Pod]]:
    config = folder / "robinauts.toml"
    config.write_text(EXAMPLE.read_text(encoding="utf-8") + WORK, encoding="utf-8")
    async with temporary_schema(applied=False) as schema:
        loaded, secret_for = load(config, os.environ)
        await init_database(schema.dsn, loaded, secret_for)
        with started("pod-a", config, schema.dsn, folder) as a:
            with started("pod-b", config, schema.dsn, folder) as b:
                yield a, b


def events(body: str) -> list[dict[str, object]]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


async def begun(http: httpx.AsyncClient, text: str = SLOW) -> tuple[str, str, asyncio.Task[str]]:
    """A turn started on that pod, read until its first words: the conversation, the run,
    and the rest of its stream, still being read."""
    request = http.build_request("POST", "/api/turns", json={"agent_id": "echo", "text": text})
    response = await http.send(request, stream=True)
    assert response.status_code == 200
    conversation = response.headers["x-robinauts-conversation-id"]
    run = response.headers["x-robinauts-run-id"]
    chunks = response.aiter_text()
    body = ""
    while '"TEXT_MESSAGE_CONTENT"' not in body:
        body += await anext(chunks)

    async def rest() -> str:
        said = body
        try:
            async for chunk in chunks:
                said += chunk
        except httpx.HTTPError:
            pass
        finally:
            await response.aclose()
        return said

    return conversation, run, asyncio.create_task(rest())


async def opened_when(
    http: httpx.AsyncClient, conversation: str, done: object, timeout: float = 20.0
) -> dict[str, object]:
    """The conversation, read again until ``done`` holds of it."""
    deadline = time.monotonic() + timeout
    while True:
        opened = (await http.get(f"/api/conversations/{conversation}")).json()
        if done(opened):  # type: ignore[operator]
            return opened
        if time.monotonic() > deadline:
            raise AssertionError(f"not yet: {opened}")
        await asyncio.sleep(0.2)


@asyncio_test
async def test_a_turn_on_one_pod_is_watched_and_stopped_through_the_other(tmp_path: Path) -> None:
    async with two_pods(tmp_path) as (a, b):
        async with (
            httpx.AsyncClient(base_url=a.url, timeout=60) as on_a,
            httpx.AsyncClient(base_url=b.url, timeout=60) as on_b,
        ):
            conversation, run, rest_on_a = await begun(on_a)
            watched = asyncio.create_task(
                on_b.get(f"/api/conversations/{conversation}/runs/{run}/events")
            )
            opened = (await on_b.get(f"/api/conversations/{conversation}")).json()
            assert opened["run_id"] == run
            stopped = await on_b.post(f"/api/conversations/{conversation}/runs/{run}/cancel")
            assert stopped.status_code == 204
            for body in (await rest_on_a, (await watched).text):
                last = events(body)[-1]
                assert last["type"] == "RUN_FINISHED"
                assert last["outcome"] == {"type": "cancelled"}
            opened = await opened_when(on_a, conversation, lambda o: o["run_id"] is None)
            assert opened["ended_badly"]["state"] == "cancelled"
        # Each line of the pod that ran it names the pod, the conversation and the turn.
        assert f"pod=pod-a conversation={conversation} turn={run} turn running" in a.said()
        assert f"pod=pod-a conversation={conversation} turn={run} turn cancelled" in a.said()


@asyncio_test
async def test_a_pod_killed_mid_turn_leaves_the_conversation_usable_through_the_other(
    tmp_path: Path,
) -> None:
    async with two_pods(tmp_path) as (a, b):
        async with (
            httpx.AsyncClient(base_url=a.url, timeout=60) as on_a,
            httpx.AsyncClient(base_url=b.url, timeout=60) as on_b,
        ):
            conversation, run, rest_on_a = await begun(on_a)
            # Two words in, and written: a piece is written within 150 ms of its arrival.
            await asyncio.sleep(2.6)
            a.process.send_signal(signal.SIGKILL)
            await rest_on_a
            # Within its lease and a little: ended interrupted, with what it had written kept
            # as a failed answer under the question.
            opened = await opened_when(on_b, conversation, lambda o: o["run_id"] is None)
            assert opened["ended_badly"]["state"] == "interrupted"
            question, answer = opened["messages"]
            assert answer["failed"] is True
            assert answer["provenance"]["run_id"] == run
            written = "".join(p["text"] for p in answer["parts"] if p["kind"] == "text")
            assert written.startswith("The tool said: slowly w0")
            replayed = await on_b.get(f"/api/conversations/{conversation}/runs/{run}/events")
            assert events(replayed.text)[-1] == {
                "type": "RUN_ERROR",
                "message": "the deployment stopped while this answer was being produced",
                "code": "interrupted",
            }
            assert (await on_b.get("/ready")).status_code == 200
        assert "turn ended interrupted: its lease, held by pod-a, had passed" in b.said()


@asyncio_test
async def test_a_pod_stopped_mid_turn_drains_and_its_streams_go_elsewhere(tmp_path: Path) -> None:
    async with two_pods(tmp_path) as (a, b):
        async with (
            httpx.AsyncClient(base_url=a.url, timeout=60) as on_a,
            httpx.AsyncClient(base_url=b.url, timeout=60) as on_b,
        ):
            conversation, run, rest_on_a = await begun(on_a)
            a.process.send_signal(signal.SIGTERM)
            body = await rest_on_a
            assert "event: CUSTOM" in body
            assert '"name":"robinauts.reconnect"' in body
            # The client attaches again through the other pod, and the turn ends there:
            # interrupted after the drain, with what it had written kept.
            last = re.findall(r"^id: (\d+)$", body, re.MULTILINE)[-1]
            again = await on_b.get(
                f"/api/conversations/{conversation}/runs/{run}/events",
                headers={"last-event-id": last},
            )
            assert events(again.text)[-1]["code"] == "interrupted"
            assert a.process.wait(timeout=30) is not None
            opened = await opened_when(on_b, conversation, lambda o: o["run_id"] is None)
            assert opened["ended_badly"]["state"] == "interrupted"
            assert opened["messages"][-1]["failed"] is True
        assert "draining" in a.said()
