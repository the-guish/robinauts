# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Two replicas on one database, as real processes: what Stage 1 promises of N of them.

Two ``robinauts start`` processes share a schema of the test PostgreSQL, and their agent is a
LangGraph agent over a stand-in vendor in this process, which streams a few words and then
holds the answer for as long as the test likes. A turn streams from one replica and is
stopped through the other; then the replica running a turn is killed, and the other ends the
turn within a few seconds, keeping what it had said, without anybody opening it.
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
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest

from chat_completions import finished, said
from controller_db import requires_postgres, url

pytestmark = requires_postgres

ROBINAUTS = Path(sys.executable).with_name("robinauts")
FIRST_WORDS = "Working on it. "
HOLD_SECONDS = 60.0


class Vendor(ThreadingHTTPServer):
    """OpenAI's Chat Completions, streaming ``FIRST_WORDS`` and then holding until released."""

    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Answer)
        self.release = threading.Event()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/v1"


class _Answer(BaseHTTPRequestHandler):
    server: Vendor

    def log_message(self, format: str, *args: Any) -> None:
        """Quiet."""

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("content-length", "0")))
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        try:
            self._send(*said(FIRST_WORDS))
            self.server.release.wait(HOLD_SECONDS)
            self._send(*said("Done."), *finished())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except OSError:
            return  # the replica that asked went away, or stopped asking

    def _send(self, *chunks: dict[str, Any]) -> None:
        for chunk in chunks:
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.flush()


CONFIG = """
[model_providers.gw]
kind = "openai-compatible"
base_url = "{base_url}"
api_key_env = "ROBINAUTS_TEST_VENDOR_KEY"

[models.m]
provider = "gw"
name = "stand-in"

[agents.slow]
title = "Slow"
system_prompt = "Take your time."
model = "m"
engine = "langchain"

[work]
lease_seconds = 2
heartbeat_seconds = 0.5
drain_seconds = 1
"""


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def with_search_path(given: str, schema: str) -> str:
    parts = urllib.parse.urlsplit(given)
    query = urllib.parse.parse_qsl(parts.query) + [("search_path", schema)]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def alone(statement: str) -> None:
    async def go() -> None:
        connection = await asyncpg.connect(url())
        try:
            await connection.execute(statement)
        finally:
            await connection.close()

    asyncio.run(go())


@contextmanager
def replicas(tmp_path: Path, vendor: Vendor) -> Iterator[dict[str, tuple[str, Any]]]:
    """Two replicas, ``a`` and ``b``, on a schema of their own, with the database set up."""
    schema = "robinauts_replicas_" + uuid.uuid4().hex
    alone(f'CREATE SCHEMA "{schema}"')
    config = tmp_path / "robinauts.toml"
    config.write_text(CONFIG.format(base_url=vendor.base_url))
    environment = {
        **os.environ,
        "ROBINAUTS_CONFIG": str(config),
        "ROBINAUTS_DATABASE_URL": with_search_path(url(), schema),
        "ROBINAUTS_TEST_VENDOR_KEY": "stand-in",
    }
    running: dict[str, tuple[str, Any]] = {}
    try:
        subprocess.run([str(ROBINAUTS), "db", "init"], env=environment, check=True)
        for name in ("a", "b"):
            port = free_port()
            process = subprocess.Popen(
                [str(ROBINAUTS), "start", "--dev-no-sign-in", "--port", str(port)],
                env={**environment, "ROBINAUTS_WORKER_ID": f"pod-{name}"},
            )
            running[name] = (f"http://127.0.0.1:{port}", process)
        for address, process in running.values():
            ready(address, process)
        yield running
    finally:
        vendor.release.set()
        for _, process in running.values():
            process.kill()
            process.wait(timeout=10)
        alone(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def ready(address: str, process: Any) -> None:
    for _ in range(300):
        if process.poll() is not None:
            raise RuntimeError(f"a replica exited with {process.returncode}")
        try:
            if httpx.get(address + "/ready").status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"{address} never became ready")


def events(lines: Iterator[str]) -> Iterator[dict[str, Any]]:
    for line in lines:
        if line.startswith("data: "):
            yield json.loads(line.removeprefix("data: "))


def until(stream: Iterator[dict[str, Any]], kind: str) -> dict[str, Any]:
    for event in stream:
        if event["type"] == kind:
            return event
    raise AssertionError(f"the stream ended before {kind}")


@pytest.fixture
def vendor() -> Iterator[Vendor]:
    serving = Vendor()
    thread = threading.Thread(target=serving.serve_forever, daemon=True)
    thread.start()
    try:
        yield serving
    finally:
        serving.release.set()
        serving.shutdown()
        serving.server_close()


def test_two_replicas_stream_stop_and_outlive_each_other(tmp_path: Path, vendor: Vendor) -> None:
    with replicas(tmp_path, vendor) as running, httpx.Client(timeout=30) as http:
        a, process_a = running["a"]
        b, _ = running["b"]

        # A turn streams from a, and is stopped through b.
        with http.stream(
            "POST", a + "/api/turns", json={"agent_id": "slow", "text": "one"}
        ) as on_a:
            run = on_a.headers["x-robinauts-run-id"]
            conversation = on_a.headers["x-robinauts-conversation-id"]
            stream = events(on_a.iter_lines())
            assert until(stream, "TEXT_MESSAGE_CONTENT")["delta"] == FIRST_WORDS
            stopped = http.post(f"{b}/api/conversations/{conversation}/runs/{run}/cancel")
            assert stopped.status_code in (202, 204)
            ending = until(stream, "RUN_FINISHED")
            assert ending["outcome"]["type"] == "cancelled"
        # b's view of the same turn says the same.
        watched = http.get(f"{b}/api/conversations/{conversation}/runs/{run}/events")
        assert '"cancelled"' in watched.text.rsplit("RUN_FINISHED", 1)[-1]

        # A second turn runs on a, which dies in the middle of it; b is watching.
        with http.stream(
            "POST", a + "/api/turns", json={"agent_id": "slow", "text": "two"}
        ) as on_a:
            run = on_a.headers["x-robinauts-run-id"]
            conversation = on_a.headers["x-robinauts-conversation-id"]
            until(events(on_a.iter_lines()), "TEXT_MESSAGE_CONTENT")
        path = f"{b}/api/conversations/{conversation}"
        with http.stream("GET", f"{path}/runs/{run}/events") as on_b:
            stream = events(on_b.iter_lines())
            assert until(stream, "TEXT_MESSAGE_CONTENT")["delta"] == FIRST_WORDS
            process_a.kill()
            started = time.monotonic()
            ending = until(stream, "RUN_ERROR")
            took = time.monotonic() - started
        assert ending["code"] == "interrupted"
        # Within a lease and a few heartbeats, not at the turn's deadline.
        assert took < 15
        opened = http.get(path).json()
        assert opened["run_id"] is None
        assert opened["ended_badly"]["state"] == "interrupted"
        left = opened["messages"][-1]
        assert left["failed"] is True
        assert left["parts"] == [{"kind": "text", "text": FIRST_WORDS}]
        # The conversation is usable on the replica that is left: Retry starts it over there.
        vendor.release.set()
        retried = http.post(f"{path}/turns", json={"retry": left["id"]})
        assert retried.status_code == 200
        assert until(events(iter(retried.text.splitlines())), "RUN_FINISHED")
        answer = http.get(path).json()["messages"][-1]
        assert (answer["failed"], answer["parts"][-1]["text"]) == (False, FIRST_WORDS + "Done.")
