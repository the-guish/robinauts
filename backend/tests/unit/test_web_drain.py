# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A stopping process: `/ready` says so, no turn is taken, and streams end with a hint to
re-attach elsewhere; the close that follows is bounded."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
from test_controller_turns import CONFIG, opened
from test_web_streams import QuietEngine, asgi_stream, body_until, headers_of

from aio import asyncio_test
from robinauts.controller.application import controller as controller_module
from robinauts.controller.composition import compose
from robinauts.controller.contract.domain import (
    DrainingError,
    Identity,
    StorageConfig,
    StorageKind,
    TurnState,
)
from robinauts.web import agui, app, cli


def composed_app() -> tuple[Any, Any]:
    composed = compose(CONFIG, storage=StorageConfig(StorageKind.IN_MEMORY), secret_for={}.get)
    web = app.create_app(
        composed.controller,
        credentials=composed.credentials,
        sign_in=None,
        secret_for={}.get,
        operations=composed.operations,
    )
    return composed, web


@asyncio_test
async def test_ready_until_the_drain_then_not_and_no_turn_is_taken() -> None:
    _, web = composed_app()
    async with web.router.lifespan_context(web):
        transport = httpx.ASGITransport(app=web)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            assert (await http.get("/ready")).json() == {"status": "ready"}
            web.state.drain()
            refused = await http.get("/ready")
            assert refused.status_code == 503
            assert refused.json()["problems"] == ["this process is stopping"]
            # Alive all the same: the process answers.
            assert (await http.get("/health")).status_code == 200
            turn = await http.post("/api/turns", json={"agent_id": "echo", "text": "hi"})
            assert turn.status_code == 503
            assert turn.json()["error"] == "DrainingError"


@asyncio_test
async def test_a_stream_ends_at_the_drain_with_a_hint_to_reattach() -> None:
    composed, web = composed_app()
    async with web.router.lifespan_context(web):
        engine = QuietEngine()
        composed.controller._engines["echo"] = engine
        posted, posting = await asgi_stream(
            web, "POST", "/api/turns", b'{"agent_id": "echo", "text": "hi"}'
        )
        await headers_of(posted)
        await body_until(posted, lambda got: "RUN_STARTED" in got)
        before = time.monotonic()
        web.state.drain()
        rest = await body_until(posted, lambda got: False)
        assert time.monotonic() - before < 2
        assert "retry: 1000\n" in rest
        said = [
            json.loads(line.removeprefix("data: "))
            for line in rest.splitlines()
            if line.startswith("data: ")
        ]
        hint = said[-1]
        assert hint == {
            "type": "CUSTOM",
            "name": agui.RECONNECT,
            "value": {"after_ms": agui.RECONNECT_AFTER_MS},
        }
        await asyncio.wait_for(posting, 5)
        # The turn carries on, here, until the close gives it its window.
        engine.gate.set()


@asyncio_test
async def test_a_turn_still_running_at_the_close_is_given_its_window_then_interrupted() -> None:
    controller = await opened()
    controller._close_timeout = 0.1
    engine = QuietEngine()
    controller._engines["echo"] = engine
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="hi")
    await asyncio.sleep(0.05)
    controller.drain()
    try:
        await controller.start_session(user, agent="echo", model="echo", text="again")
    except DrainingError:
        pass
    else:
        raise AssertionError("a draining process took a turn")
    assert await controller.readiness() == ("this process is stopping",)
    await controller.close()
    turn = await controller._store.get_turn(user.id, started.session_id, started.turn_id)
    assert turn is not None
    assert turn.state is TurnState.INTERRUPTED


@asyncio_test
async def test_the_close_does_not_wait_for_ever_on_a_store_that_hangs(monkeypatch: Any) -> None:
    monkeypatch.setattr(controller_module, "FINAL_WAIT", 0.1)
    controller = await opened()

    async def hangs() -> None:
        await asyncio.Event().wait()

    controller._store.close = hangs
    before = time.monotonic()
    await controller.close()
    assert time.monotonic() - before < 2


def test_a_stop_signal_starts_the_drain_before_uvicorn_stops() -> None:
    drained: list[str] = []

    async def go() -> None:
        config = cli.uvicorn.Config(app=None, host="127.0.0.1", port=0)
        server = cli.DrainingServer(config, lambda: drained.append("drained"))
        server._loop = asyncio.get_running_loop()
        server.handle_exit(15, None)
        assert server.should_exit
        await asyncio.sleep(0)

    asyncio.run(go())
    assert drained == ["drained"]


@asyncio_test
async def test_a_pool_with_nothing_to_give_is_a_503() -> None:
    composed, web = composed_app()

    async def busy() -> None:
        raise TimeoutError

    async with web.router.lifespan_context(web):
        composed.controller.list_agents = busy
        transport = httpx.ASGITransport(app=web)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            answered = await http.get("/api/agents")
        assert answered.status_code == 503
        assert answered.json() == {"error": "Busy", "detail": app.BUSY}
