# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A sandboxed agent's conversations, run by a sandbox worker that reaches the server over the
worker API alone (``docs/architecture/sandbox-workers.md``).

The server is the app over the in-memory store; the sandbox worker is the worker composed on
the HTTP store, whose requests reach the app through an ASGI transport. Nothing else joins
them: what the sandbox worker knows of a session is what the worker API answered.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections import Counter
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx

from robinauts.agent_engines.contract.domain import Done, Event, TextDelta
from robinauts.agent_engines.echo_engine.engine import EchoEngine
from robinauts.controller.application.worker import Worker
from robinauts.controller.composition import compose, compose_sandbox_worker
from robinauts.controller.contract.domain import (
    AgentConfig,
    Config,
    ModelConfig,
    ProviderConfig,
    ProviderKind,
    SandboxConfig,
    StorageConfig,
    StorageKind,
    WorkConfig,
)
from robinauts.controller.contract.worker_wire import PREFIX, SANDBOX_HEADER
from robinauts.web.app import create_app
from util.aio import asyncio_test

TOKEN = "the-workers-token"
SECRETS = {"SANDBOX_TOKEN": TOKEN}

CONFIG = Config(
    providers={"echo": ProviderConfig("echo", ProviderKind.ANTHROPIC, "ECHO_API_KEY")},
    models={"echo": ModelConfig("echo", provider="echo", name="echo", title="Echo")},
    agents={
        "echo": AgentConfig("echo", title="Echo", system_prompt="", model="echo", engine="echo"),
        "coder": AgentConfig(
            "coder", title="Coder", system_prompt="", model="echo", engine="echo", sandboxed=True
        ),
    },
    work=WorkConfig(lease_seconds=3.0, heartbeat_seconds=0.2),
    sandbox=SandboxConfig("SANDBOX_TOKEN", long_poll_seconds=0.2, gone_after_seconds=1.0),
)


class CountingEngine(EchoEngine):
    """The echo, told by how many turns of the session this process has run: its memory is
    this process's, as a sandbox's engines' is. ``gate``, when set, holds every turn until it
    opens."""

    def __init__(self) -> None:
        self.turns: Counter[uuid.UUID] = Counter()
        self.gate: asyncio.Event | None = None

    async def stream(  # type: ignore[override]
        self, session_id: uuid.UUID, *args: Any, **kwargs: Any
    ) -> AsyncGenerator[Event, None]:
        self.turns[session_id] += 1
        yield TextDelta(text=f"turn {self.turns[session_id]}. ")
        if self.gate is not None:
            await self.gate.wait()
        async for event in super().stream(session_id, *args, **kwargs):
            if isinstance(event, Done):
                text = f"turn {self.turns[session_id]}. {event.text}"
                yield Done(text=text, checkpoint_id=event.checkpoint_id)
            else:
                yield event


@dataclass
class Deployment:
    browser: httpx.AsyncClient
    """Web's routes, as the browser calls them."""
    transport: httpx.ASGITransport
    """The server, as a sandbox reaches it."""

    @asynccontextmanager
    async def sandbox(self) -> AsyncIterator[tuple[Worker, CountingEngine]]:
        """A sandbox worker, started, with the engine it runs every turn on."""
        engine = CountingEngine()
        worker = compose_sandbox_worker(
            CONFIG,
            api_url="http://api.internal",
            secret_for=SECRETS.get,
            engines={"echo": lambda *_: engine},
            transport=self.transport,
        )
        await worker.start()
        try:
            yield worker, engine
        finally:
            await worker.stop()


@asynccontextmanager
async def deployment() -> AsyncIterator[Deployment]:
    composed = compose(CONFIG, storage=StorageConfig(StorageKind.IN_MEMORY), secret_for=SECRETS.get)
    app = create_app(composed, sign_in=None, secret_for=SECRETS.get)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as browser:
            yield Deployment(browser, transport)


def events(body: str) -> list[dict[str, Any]]:
    return [
        json.loads(line.removeprefix("data: "))
        for block in body.split("\n\n")
        for line in block.splitlines()
        if line.startswith("data: ")
    ]


def text_of(body: str) -> str:
    return "".join(e["delta"] for e in events(body) if e["type"] == "TEXT_MESSAGE_CONTENT")


async def ask(http: httpx.AsyncClient, cid: str, text: str) -> httpx.Response:
    opened = (await http.get(f"/api/conversations/{cid}")).json()
    parent = opened["messages"][-1]["id"]
    return await http.post(
        f"/api/conversations/{cid}/turns", json={"text": text, "parent_id": parent}
    )


def worker_client(transport: httpx.ASGITransport, token: str = TOKEN) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=transport,
        base_url="http://api.internal" + PREFIX,
        headers={"Authorization": f"Bearer {token}", SANDBOX_HEADER: str(uuid.uuid4())},
    )


@asyncio_test
async def test_a_sandboxed_conversation_runs_in_the_sandbox_and_goes_on_from_its_memory() -> None:
    async with deployment() as deployed, deployed.sandbox() as (worker, engine):
        http = deployed.browser
        started = await http.post("/api/turns", json={"agent_id": "coder", "text": "hello"})
        assert started.status_code == 200
        assert [e["type"] for e in events(started.text)][-1] == "RUN_FINISHED"
        assert text_of(started.text) == "turn 1. The tool said: hello"
        cid = started.headers["x-robinauts-conversation-id"]

        # The follow-up is the same sandbox's, which remembers the first turn.
        followed = await ask(http, cid, "and again")
        assert text_of(followed.text) == "turn 2. The tool said: and again"
        assert engine.turns[uuid.UUID(cid)] == 2

        opened = (await http.get(f"/api/conversations/{cid}")).json()
        assert [m["role"] for m in opened["messages"]] == ["user", "assistant"] * 2
        texts = [p["text"] for p in opened["messages"][-1]["parts"] if p["kind"] == "text"]
        assert "".join(texts) == "turn 2. The tool said: and again"
        assert opened["sandbox_gone"] is False

        # The server's own worker still answers an agent that is not sandboxed, and never
        # passes its turns to the sandbox.
        plain = await http.post("/api/turns", json={"agent_id": "echo", "text": "hi"})
        assert text_of(plain.text) == "The tool said: hi"
        assert len(engine.turns) == 1


@asyncio_test
async def test_a_sandbox_that_is_gone_leaves_its_conversation_read_only() -> None:
    async with deployment() as deployed:
        http = deployed.browser
        async with deployed.sandbox():
            started = await http.post("/api/turns", json={"agent_id": "coder", "text": "hello"})
            cid = started.headers["x-robinauts-conversation-id"]
        # Another sandbox starts, but the memory of this conversation went with the first.
        async with deployed.sandbox() as (_, engine):
            await asyncio.sleep(CONFIG.sandbox.gone_after_seconds + 0.5)
            refused = await ask(http, cid, "are you there?")
            assert refused.status_code == 409
            assert refused.json()["error"] == "SandboxGoneError"
            opened = (await http.get(f"/api/conversations/{cid}")).json()
            assert opened["sandbox_gone"] is True
            assert len(opened["messages"]) == 2
            # A new conversation is the new sandbox's.
            fresh = await http.post("/api/turns", json={"agent_id": "coder", "text": "hi"})
            assert text_of(fresh.text) == "turn 1. The tool said: hi"
            assert len(engine.turns) == 1


@asyncio_test
async def test_a_cancel_reaches_the_sandbox_with_its_lease() -> None:
    async with deployment() as deployed, deployed.sandbox() as (_, engine):
        http = deployed.browser
        engine.gate = asyncio.Event()
        turn = asyncio.create_task(
            http.post("/api/turns", json={"agent_id": "coder", "text": "slowly"})
        )
        while not engine.turns:
            await asyncio.sleep(0.05)
        (session,) = engine.turns
        opened = (await http.get(f"/api/conversations/{session}")).json()
        cancelled = await http.post(f"/api/conversations/{session}/runs/{opened['run_id']}/cancel")
        assert cancelled.status_code == 204
        body = (await asyncio.wait_for(turn, 10)).text
        # A cancel is a run that finished, as for any turn; the record says how.
        assert [e["type"] for e in events(body)][-1] == "RUN_FINISHED"
        assert "The tool said" not in text_of(body)
        ended = (await http.get(f"/api/conversations/{session}")).json()["ended_badly"]
        assert ended["state"] == "cancelled"


@asyncio_test
async def test_the_worker_api_takes_the_workers_token_and_its_own_sessions_alone() -> None:
    async with deployment() as deployed:
        transport = deployed.transport
        async with worker_client(transport, token="not-the-token") as stranger:
            assert (await stranger.get("/task")).status_code == 401
        # A browser's local user is no sandbox either.
        assert (await deployed.browser.get(PREFIX + "/task")).status_code == 401

        # A sandboxed turn queued while no sandbox runs; another sandbox than the one that
        # claims it reaches none of it.
        http = deployed.browser
        asked = asyncio.create_task(
            http.post("/api/turns", json={"agent_id": "coder", "text": "hello"})
        )
        async with worker_client(transport) as claimer, worker_client(transport) as other:
            claimed = await claimer.get("/task", params={"wait": "5"})
            assert claimed.status_code == 200
            task = claimed.json()["task"]
            turn = task["payload"]["turn_id"]
            assert (await other.get(f"/turns/{turn}")).status_code == 404
            assert (await other.get("/task")).status_code == 204

            loaded = (await claimer.get(f"/turns/{turn}")).json()
            assert "owner_id" not in loaded["session"]
            question = loaded["messages"][0]
            start = {
                "v": 1,
                "kind": "message_started",
                "turn_id": turn,
                "position": 1,
                "message_id": str(uuid.uuid4()),
                "parent_id": question["id"],
                "role": "user",
            }
            # A message as the user, an unknown key, a gap and a turn's end are refused.
            refusals = [
                [start],
                [start | {"role": "assistant", "extra": "kept?"}],
                [start | {"role": "assistant", "position": 2}],
                [
                    {
                        "v": 1,
                        "kind": "turn_ended",
                        "turn_id": turn,
                        "position": 1,
                        "state": "finished",
                    }
                ],
            ]
            for refused in refusals:
                answer = await claimer.post(f"/turns/{turn}/events", json={"events": refused})
                assert answer.status_code == 422, refused
            accepted = await claimer.post(
                f"/turns/{turn}/events", json={"events": [start | {"role": "assistant"}]}
            )
            assert accepted.status_code == 204
            assert (await other.post("/lease", json={"tasks": [task["id"]]})).json() == {
                "cancel": []
            }
            finished = await claimer.post(
                f"/turns/{turn}/finish",
                json={
                    "state": "failed",
                    "error": "given up",
                    "answer": None,
                    "events": [
                        {
                            "v": 1,
                            "kind": "turn_ended",
                            "turn_id": turn,
                            "position": 2,
                            "state": "failed",
                        }
                    ],
                },
            )
            assert finished.status_code == 204
            again = await claimer.post(f"/turns/{turn}/finish", json={"state": "failed"})
            assert again.status_code == 409
        body = (await asyncio.wait_for(asked, 10)).text
        assert [e["type"] for e in events(body)][-1] == "RUN_ERROR"
