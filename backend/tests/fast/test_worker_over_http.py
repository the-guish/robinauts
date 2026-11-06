# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A worker that reaches the core over HTTP alone: the API's ``/internal/worker/`` routes
over the controller on the in-memory store, and the client a worker process runs, with the
app in place of the network. ``tests/api`` runs the same over a real worker process."""

from __future__ import annotations

import asyncio
import logging
import socket
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from unittest.mock import create_autospec

import httpx
import pytest
from echo_controller import CONFIG, wait_for_turn_end

from robinauts.agent_engines.contract.domain import Event
from robinauts.agent_engines.contract.ports import AgentEngine
from robinauts.agent_engines.echo_engine.engine import EchoEngine
from robinauts.controller.application.worker import Worker
from robinauts.controller.composition import Composed, compose, compose_worker
from robinauts.controller.contract.domain import (
    Identity,
    SessionNotFoundError,
    StorageConfig,
    StorageKind,
    TextPart,
    TurnLostError,
    TurnState,
)
from robinauts.web.app import create_app
from robinauts.web.lifecycle import Lifecycle
from robinauts.web.worker_api import WorkerAccess
from robinauts.web.worker_client import HttpTurnBroker, QuietRequests
from robinauts.web.worker_wire import PREFIX
from util.aio import asyncio_test

SECRET = "the-worker-s-secret"
MEMORY = StorageConfig(StorageKind.IN_MEMORY)


@dataclass
class OverHttp:
    composed: Composed
    access: WorkerAccess
    app: Any
    broker: HttpTurnBroker
    worker: Worker


@asynccontextmanager
async def over_http(engine: AgentEngine | None = None) -> AsyncIterator[OverHttp]:
    """The server's half started as ``Lifecycle`` starts it for a worker process, and a
    worker over HTTP to it, started; both stopped after."""
    engines = None if engine is None else {"echo": lambda *_: engine}
    composed = compose(CONFIG, storage=MEMORY, secret_for={}.get, engines=engines)
    app = create_app(composed, sign_in=None, secret_for={}.get)
    lifecycle: Lifecycle = app.state.lifecycle
    await composed.controller.open()
    lifecycle.access.open(SECRET)
    await composed.broker.listen_for_cancels(lifecycle.access.stop_requested)
    broker = HttpTurnBroker("http://127.0.0.1", SECRET, transport=httpx.ASGITransport(app=app))
    worker = compose_worker(
        CONFIG, storage=MEMORY, secret_for={}.get, broker=broker, engines=engines
    )
    await worker.start()
    try:
        yield OverHttp(composed, lifecycle.access, app, broker, worker)
    finally:
        await worker.stop()
        lifecycle.access.close()
        await composed.controller.close()


@asynccontextmanager
async def raw(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as http:
        yield http


def gated_engine() -> tuple[Any, asyncio.Event]:
    """The echo engine, mocked: it streams the echo's turn once the gate is set."""
    echo = EchoEngine()
    gate = asyncio.Event()
    engine = create_autospec(AgentEngine, spec_set=True, instance=True)
    engine.kinds.side_effect = echo.kinds

    async def stream(*args: Any, **kwargs: Any) -> AsyncGenerator[Event, None]:
        await gate.wait()
        async for event in echo.stream(*args, **kwargs):
            yield event

    engine.stream.side_effect = stream
    return engine, gate


@asyncio_test
async def test_a_worker_over_http_runs_a_turn_to_its_answer() -> None:
    async with over_http() as running:
        controller = running.composed.controller
        user = await controller.ensure_user(Identity("local", "me"))
        started = await controller.start_session(user, agent="echo", model="echo", text="hello")
        await asyncio.wait_for(wait_for_turn_end(controller, user, started), 10)
        opened = await controller.open_session(user, started.session_id)
        question, answer = opened.messages
        assert question == started.question
        assert answer.parent_id == question.id
        assert any(isinstance(p, TextPart) and "hello" in p.text for p in answer.parts)
        assert opened.ended_badly is None


@asyncio_test
async def test_a_cancel_reaches_a_worker_over_http() -> None:
    engine, gate = gated_engine()
    async with over_http(engine) as running:
        controller: Any = running.composed.controller
        user = await controller.ensure_user(Identity("local", "me"))
        started = await controller.start_session(user, agent="echo", model="echo", text="one")
        sid = started.session_id
        await controller._store.wait_for_events(user.id, sid, started.turn_id, 0, 5.0)
        await controller.cancel_turn(user, sid, started.turn_id)
        await asyncio.wait_for(wait_for_turn_end(controller, user, started), 10)
        turn = await controller._store.get_turn(user.id, sid, started.turn_id)
        assert turn is not None
        assert turn.state is TurnState.CANCELLED
        gate.set()


@asyncio_test
async def test_the_refusals_a_worker_acts_on_cross_as_themselves() -> None:
    async with over_http() as running:
        controller = running.composed.controller
        user = await controller.ensure_user(Identity("local", "me"))
        with pytest.raises(SessionNotFoundError):
            await running.broker.get_session(user.id, uuid.uuid4())
        started = await controller.start_session(user, agent="echo", model="echo", text="hello")
        await asyncio.wait_for(wait_for_turn_end(controller, user, started), 10)
        now = datetime.now(UTC)
        with pytest.raises(TurnLostError):
            await running.broker.append_event(
                user.id, started.session_id, started.turn_id, 99, {"v": 1}, now, now
            )
        assert await running.broker.find_turn(uuid.uuid4()) is None
        assert await running.broker.get_task(uuid.uuid4()) is None


@asyncio_test
async def test_only_the_running_workers_secret_is_let_in() -> None:
    async with over_http() as running, raw(running.app) as http:
        claim = {"now": "2026-11-06T12:00:00Z", "until": "2026-11-06T12:01:30Z", "limit": 1}
        path = f"{PREFIX}/tasks/claim"
        assert (await http.post(path, json=claim)).status_code == 401
        wrong = {"Authorization": "Bearer not-the-secret"}
        assert (await http.post(path, json=claim, headers=wrong)).status_code == 401
        right = {"Authorization": f"Bearer {SECRET}"}
        assert (await http.post(path, json=claim, headers=right)).json() == {"tasks": []}
        running.access.close()
        assert (await http.post(path, json=claim, headers=right)).status_code == 404


@asyncio_test
async def test_with_no_worker_process_the_routes_are_not_there() -> None:
    composed = compose(CONFIG, storage=MEMORY, secret_for={}.get)
    app = create_app(composed, sign_in=None, secret_for={}.get)
    async with app.router.lifespan_context(app), raw(app) as http:
        answer = await http.get(f"{PREFIX}/cancels", headers={"Authorization": "Bearer x"})
        assert answer.status_code == 404
    assert "/internal" not in str(app.openapi()["paths"])


@asyncio_test
async def test_a_wait_ends_as_soon_as_the_worker_is_let_go() -> None:
    async with over_http() as running:
        waiting = asyncio.ensure_future(running.broker.wait_for_queued(datetime.now(UTC), 25.0))
        cancels = asyncio.ensure_future(running.access.cancels_after(0, 25.0))
        await asyncio.sleep(0.1)
        assert not waiting.done()
        running.access.close()
        assert await asyncio.wait_for(waiting, 2) is False
        assert await asyncio.wait_for(cancels, 2) == (0, [])


@asyncio_test
async def test_a_poll_hears_the_cancels_past_its_cursor_alone() -> None:
    access = WorkerAccess(keep=2)
    access.open(SECRET)
    first, second, third = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    assert await access.cancels_after(None, 0) == (0, [])
    waiting = asyncio.ensure_future(access.cancels_after(0, 5))
    await asyncio.sleep(0)
    access.stop_requested(first)
    assert await asyncio.wait_for(waiting, 1) == (1, [first])
    access.stop_requested(second)
    access.stop_requested(third)
    # The oldest beyond what is kept is gone: the next renewal of the leases names it.
    assert await access.cancels_after(0, 0) == (3, [second, third])
    assert await access.cancels_after(3, 0) == (3, [])
    # A cursor this server never gave, as after its restart: the cursor alone.
    assert await access.cancels_after(7, 0) == (3, [])


@asyncio_test
async def test_a_refused_connection_is_tried_again_for_a_while_alone() -> None:
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        port = unused.getsockname()[1]
    broker = HttpTurnBroker(f"http://127.0.0.1:{port}", SECRET, connect_patience=0.3)
    loop = asyncio.get_running_loop()
    began = loop.time()
    with pytest.raises(httpx.ConnectError):
        await broker.get_task(uuid.uuid4())
    assert 0.1 <= loop.time() - began < 3
    await broker.close()


@asyncio_test
async def test_a_worker_over_http_stops_at_once_mid_wait() -> None:
    loop = asyncio.get_running_loop()
    async with over_http() as running:
        # The worker's claim loop and its poll for cancels are both mid-wait here.
        await asyncio.sleep(0.2)
        stopping = loop.time()
    assert loop.time() - stopping < 2
    assert running.access.is_open is False


def test_the_worker_logs_none_of_its_requests_to_the_core() -> None:
    quiet = QuietRequests("http://127.0.0.1:8000/")
    line = 'HTTP Request: %s %s "%s %d %s"'

    def record(url: str) -> logging.LogRecord:
        args = ("PUT", httpx.URL(url), "HTTP/1.1", 204, "No Content")
        return logging.LogRecord("httpx", logging.INFO, __file__, 1, line, args, None)

    assert not quiet.filter(record(f"http://127.0.0.1:8000{PREFIX}/tasks/claim"))
    assert quiet.filter(record("http://127.0.0.1:44379/v1/chat/completions"))
    assert quiet.filter(record("http://127.0.0.1:8000/api/turns"))
