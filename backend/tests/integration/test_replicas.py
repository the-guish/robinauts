# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Two processes of the app on one database, as two replicas behind a load balancer.

Each is a real ``robinauts start``, in a process of its own, on a port of its own, with a
worker id of its own. The model is a stand-in vendor speaking OpenAI's Chat Completions in
this test's process, which answers with a little text and then holds the rest until the test
lets it go, so that a turn is going for as long as the test needs it to be. What is shown:

- a turn's stream read through the other process;
- Stop asked of the process that does not run the turn, which stops it;
- a process killed in the middle of a turn: the other ends it as interrupted once its lease
  has passed, with what it did as a failed answer, which Retry starts again there;
- a process stopped with ``SIGTERM``: its stream ends with a hint to re-attach, and its turn
  is ended as interrupted within the drain.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import asyncpg
import httpx

from aio import asyncio_test
from chat_completions import finished, said, streamed
from controller_db import requires_postgres, temporary_schema, url
from robinauts.controller.composition import init_database, load

pytestmark = requires_postgres

READY_SECONDS = 60.0
KEY = "ROBINAUTS_TEST_VENDOR_KEY"
FIRST = "Thinking about it. "
REST = "Done."


class Vendor:
    """OpenAI's Chat Completions, streamed: ``FIRST`` at once, ``REST`` once let go."""

    def __init__(self) -> None:
        self.go = threading.Event()
        vendor = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 -- the handler's own name
                self.rfile.read(int(self.headers.get("content-length", 0)))
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                try:
                    first = streamed(*said(FIRST)).removesuffix(b"data: [DONE]\n\n")
                    self.wfile.write(first)
                    self.wfile.flush()
                    vendor.go.wait(60)
                    rest = [{**c, "choices": c["choices"]} for c in said("x")]
                    rest[0]["choices"][0]["delta"] = {"content": REST}
                    self.wfile.write(streamed(*rest, *finished()))
                    self.wfile.flush()
                except OSError:
                    return

            def log_message(self, *args: Any) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.go.set()
        self.server.shutdown()
        self.server.server_close()


def free_port() -> int:
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        return held.getsockname()[1]


def configuration(path: Path, vendor: Vendor) -> Path:
    path.write_text(f"""
[model_providers.vendor]
kind = "openai-compatible"
base_url = "http://127.0.0.1:{vendor.port}/v1"
api_key_env = "{KEY}"

[models.slow]
provider = "vendor"
name = "slow-1"
max_retries = 0

[agents.assistant]
title = "Assistant"
system_prompt = "Answer."
model = "slow"
engine = "pydantic-ai"

[work]
lease_seconds = 3
heartbeat_seconds = 0.5
drain_seconds = 1
""")
    return path


@contextmanager
def pod(name: str, config: Path, dsn: str) -> Iterator[tuple[subprocess.Popen[bytes], str]]:
    port = free_port()
    env = {
        **os.environ,
        "ROBINAUTS_CONFIG": str(config),
        "ROBINAUTS_DATABASE_URL": dsn,
        "ROBINAUTS_WORKER_ID": name,
        KEY: "not-a-real-key",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    command = [
        sys.executable,
        "-c",
        "from robinauts.web.cli import run; run()",
        "start",
        "--dev-no-sign-in",
        "--port",
        str(port),
    ]
    process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        yield process, f"http://127.0.0.1:{port}"
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(10)


async def ready(http: httpx.AsyncClient, base: str, process: subprocess.Popen[bytes]) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + READY_SECONDS
    while loop.time() < deadline:
        if process.poll() is not None:
            said_ = process.stdout.read().decode() if process.stdout else ""
            raise AssertionError(f"the process stopped: {said_}")
        try:
            if (await http.get(base + "/ready")).status_code == 200:
                return
        except httpx.TransportError:
            pass
        await asyncio.sleep(0.2)
    raise AssertionError(f"{base} was not ready in time")


class Watched:
    """A stream being read in the background, line by line."""

    def __init__(self, http: httpx.AsyncClient, method: str, target: str, **kwargs: Any):
        self.lines: list[str] = []
        self.headers: dict[str, str] = {}
        self.status = 0
        self._opened = asyncio.Event()
        self._changed = asyncio.Event()
        self.task = asyncio.create_task(self._read(http, method, target, kwargs))

    async def _read(self, http: httpx.AsyncClient, method: str, target: str, kwargs: Any) -> None:
        async with http.stream(method, target, **kwargs) as response:
            self.status = response.status_code
            self.headers = dict(response.headers)
            self._opened.set()
            async for line in response.aiter_lines():
                self.lines.append(line)
                self._changed.set()
        self._changed.set()

    async def opened(self) -> dict[str, str]:
        await asyncio.wait_for(self._opened.wait(), 30)
        assert self.status == 200, self.status
        return self.headers

    async def until(self, text: str, timeout: float = 30) -> None:
        async def seen() -> None:
            while not any(text in line for line in self.lines):
                if self.task.done():
                    raise AssertionError(f"{text!r} never came: {self.lines}")
                self._changed.clear()
                await self._changed.wait()

        await asyncio.wait_for(seen(), timeout)

    async def ended(self, timeout: float = 30) -> list[str]:
        await asyncio.wait_for(asyncio.shield(self.task), timeout)
        return self.lines


@asynccontextmanager
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(trust_env=False, timeout=30) as http:
        yield http


@asyncio_test
async def test_two_processes_serve_one_deployment(tmp_path: Path) -> None:
    vendor = Vendor()
    try:
        async with temporary_schema(applied=False) as schema:
            dsn = f"{url()}{'&' if '?' in url() else '?'}search_path={schema.name}"
            config_path = configuration(tmp_path / "robinauts.toml", vendor)
            config, secret_for = load(config_path, {KEY: "x"})
            await init_database(dsn, config, secret_for)
            with (
                pod("pod-a", config_path, dsn) as (a, at_a),
                pod("pod-b", config_path, dsn) as (b, at_b),
            ):
                async with client() as http:
                    await ready(http, at_a, a)
                    await ready(http, at_b, b)
                    await stream_and_stop_across(http, at_a, at_b)
                    await a_killed_process(http, a, at_a, at_b, vendor, dsn)
                    await a_drained_process(http, b, at_b, vendor, dsn)
    finally:
        vendor.close()


async def stream_and_stop_across(http: httpx.AsyncClient, at_a: str, at_b: str) -> None:
    started = Watched(
        http, "POST", at_a + "/api/turns", json={"agent_id": "assistant", "text": "why?"}
    )
    headers = await started.opened()
    run, conversation = headers["x-robinauts-run-id"], headers["x-robinauts-conversation-id"]
    await started.until(FIRST)
    # The same turn's stream, through the process that does not run it.
    elsewhere = Watched(http, "GET", f"{at_b}/api/conversations/{conversation}/runs/{run}/events")
    await elsewhere.opened()
    await elsewhere.until(FIRST)
    # Stop, through that other process: it reaches the turn where it runs.
    stopped = await http.post(f"{at_b}/api/conversations/{conversation}/runs/{run}/cancel")
    assert stopped.status_code == 204
    for watched in (started, elsewhere):
        said_ = "\n".join(await watched.ended())
        assert '"outcome":{"type":"cancelled"}' in said_.replace(" ", ""), said_


async def a_killed_process(
    http: httpx.AsyncClient,
    a: subprocess.Popen[bytes],
    at_a: str,
    at_b: str,
    vendor: Vendor,
    dsn: str,
) -> None:
    started = Watched(
        http, "POST", at_a + "/api/turns", json={"agent_id": "assistant", "text": "and?"}
    )
    conversation = (await started.opened())["x-robinauts-conversation-id"]
    await started.until(FIRST)
    a.send_signal(signal.SIGKILL)
    a.wait(10)
    started.task.cancel()
    # The other process ends it once its lease has passed, keeping what it did.
    opened: dict[str, Any] = {}
    for _ in range(100):
        opened = (await http.get(f"{at_b}/api/conversations/{conversation}")).json()
        if opened["ended_badly"] is not None:
            break
        await asyncio.sleep(0.2)
    assert opened["ended_badly"]["state"] == "interrupted"
    assert opened["run_id"] is None
    answer = opened["messages"][-1]
    assert answer["failed"] is True
    assert answer["parts"] == [{"kind": "text", "text": FIRST}]
    # Retry, on the process that is left, starts the task again and finishes it.
    vendor.go.set()
    retried = Watched(
        http,
        "POST",
        f"{at_b}/api/conversations/{conversation}/turns",
        json={"retry": answer["id"]},
    )
    await retried.opened()
    said_ = "\n".join(await retried.ended())
    assert REST in said_
    assert "RUN_FINISHED" in said_
    vendor.go.clear()


async def a_drained_process(
    http: httpx.AsyncClient,
    b: subprocess.Popen[bytes],
    at_b: str,
    vendor: Vendor,
    dsn: str,
) -> None:
    started = Watched(
        http, "POST", at_b + "/api/turns", json={"agent_id": "assistant", "text": "now?"}
    )
    headers = await started.opened()
    run = headers["x-robinauts-run-id"]
    await started.until(FIRST)
    b.send_signal(signal.SIGTERM)
    said_ = "\n".join(await started.ended(10))
    assert "robinauts.reconnect" in said_
    assert "retry: 1000" in said_
    # uvicorn stops gracefully, then raises the signal again: it ends as the signal says.
    assert b.wait(30) in (0, -signal.SIGTERM)
    output = b.stdout.read().decode() if b.stdout else ""
    assert "pod=pod-b" in output
    connection = await asyncpg.connect(dsn)
    try:
        state = await connection.fetchval("SELECT state FROM turns WHERE id = $1::uuid", run)
        answer = await connection.fetchval(
            "SELECT document FROM messages WHERE document->>'turn_id' = $1", run
        )
    finally:
        await connection.close()
    assert state == "interrupted"
    assert json.loads(answer)["failed"] is True


def test_the_vendor_holds_what_it_was_told_to() -> None:
    # The stand-in's own two halves, as the test above relies on them.
    vendor = Vendor()
    try:
        with httpx.Client(trust_env=False) as http:
            with http.stream(
                "POST", f"http://127.0.0.1:{vendor.port}/v1/chat/completions", json={}
            ) as answered:
                lines = answered.iter_lines()
                assert FIRST in next(lines)
                vendor.go.set()
                rest = "\n".join(lines)
        assert REST in rest
        assert rest.strip().endswith("data: [DONE]")
    finally:
        vendor.close()
