# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A process that is stopping: not ready, no new turn, and its streams sent elsewhere."""

from __future__ import annotations

import asyncio
import json
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import uvicorn
from test_controller_runner import GatedEngine
from test_controller_turns import CONFIG

from aio import asyncio_test
from robinauts.controller.composition import Composed, compose
from robinauts.controller.contract.domain import StorageConfig, StorageKind
from robinauts.web.agui import kept_alive, reconnect_hint
from robinauts.web.app import LOCAL_IDENTITY, create_app
from robinauts.web.cli import DrainingServer


@asynccontextmanager
async def served() -> AsyncIterator[tuple[httpx.AsyncClient, Any, Composed]]:
    composed = compose(CONFIG, storage=StorageConfig(StorageKind.IN_MEMORY), secret_for={}.get)
    app = create_app(
        composed.controller,
        credentials=composed.credentials,
        sign_in=None,
        secret_for={}.get,
        operations=composed.operations,
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http, app, composed


@asyncio_test
async def test_ready_until_the_drain_and_then_no_new_turn() -> None:
    async with served() as (http, app, _):
        ready = await http.get("/ready")
        assert (ready.status_code, ready.json()) == (200, {"status": "ready"})
        app.state.drain()
        await asyncio.sleep(0)
        unready = await http.get("/ready")
        assert unready.status_code == 503
        assert unready.json()["problems"] == ["this process is stopping"]
        assert (await http.get("/health")).status_code == 200
        refused = await http.post("/api/turns", json={"agent_id": "echo", "text": "hello"})
        assert refused.status_code == 503
        assert refused.json()["error"] == "DrainingError"


@asyncio_test
async def test_a_stream_during_the_drain_ends_with_a_hint_to_reconnect_elsewhere() -> None:
    async with served() as (http, app, composed):
        controller = composed.controller
        engine = GatedEngine()
        controller._engines["echo"] = engine  # type: ignore[attr-defined]
        user = await controller.ensure_user(LOCAL_IDENTITY)
        started = await controller.start_session(user, agent="echo", model="echo", text="hi")
        app.state.drain()
        url = f"/api/conversations/{started.session_id}/runs/{started.turn_id}/events"
        body = (await asyncio.wait_for(http.get(url), 5.0)).text
        assert body.endswith(reconnect_hint())
        assert "retry: 1000\n\n" in body
        custom = [
            json.loads(line.removeprefix("data: "))
            for line in body.splitlines()
            if line.startswith("data: ") and "CUSTOM" in line
        ]
        assert custom == [
            {"type": "CUSTOM", "name": "robinauts.reconnect", "value": {"after_ms": 1000}}
        ]
        assert "RUN_FINISHED" not in body
        # The turn goes on: the drain ended the stream, not the run.
        engine.gate.set()
        async for _ in controller.watch_turn(user, started.session_id, started.turn_id):
            pass


@asyncio_test
async def test_a_stream_ends_with_the_hint_as_soon_as_the_drain_begins() -> None:
    drained = asyncio.Event()

    async def quiet() -> AsyncIterator[str]:
        yield "first"
        await asyncio.sleep(3600)
        yield "never"

    said = []
    async for chunk in kept_alive(quiet(), 10.0, drained):
        said.append(chunk)
        drained.set()
    assert said == ["first", reconnect_hint()]


def test_the_first_stop_signal_begins_the_drain_once_and_uvicorn_still_stops() -> None:
    drained: list[str] = []

    async def go() -> None:
        server = DrainingServer(uvicorn.Config(app=None), lambda: drained.append("drain"))
        server.handle_exit(signal.SIGTERM, None)
        server.handle_exit(signal.SIGTERM, None)
        await asyncio.sleep(0)
        assert server.should_exit

    asyncio.run(go())
    assert drained == ["drain"]
