# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A replica that stops: it says it is not ready, takes no turn, and sends its streams away."""

from __future__ import annotations

import asyncio
import dataclasses
import json

import httpx
from test_controller_runner import GatedEngine
from test_controller_turns import CONFIG

from aio import asyncio_test
from robinauts.controller.composition import compose
from robinauts.controller.contract.domain import (
    Identity,
    StorageConfig,
    StorageKind,
    TurnState,
    WorkConfig,
)
from robinauts.web import agui
from robinauts.web.app import create_app


@asyncio_test
async def test_a_draining_replica_is_not_ready_takes_no_turn_and_ends_its_streams() -> None:
    config = dataclasses.replace(CONFIG, work=WorkConfig(drain_seconds=0.2))
    composed = compose(config, storage=StorageConfig(StorageKind.IN_MEMORY), secret_for={}.get)
    app = create_app(
        composed.controller,
        credentials=composed.credentials,
        sign_in=None,
        secret_for={}.get,
        operations=composed.operations,
    )
    async with app.router.lifespan_context(app):
        engine = GatedEngine()
        composed.controller._engines["echo"] = engine  # type: ignore[attr-defined]
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            assert (await http.get("/ready")).json() == {"status": "ready"}
            # httpx's ASGI transport hands the body over whole: the drain begins while the
            # request is in the middle of its stream.
            drains: list[asyncio.Task[None]] = []
            asyncio.get_running_loop().call_later(
                0.2, lambda: drains.append(asyncio.ensure_future(app.state.drain()))
            )
            streamed = await http.post("/api/turns", json={"agent_id": "echo", "text": "hi"})
            run_id = streamed.headers["x-robinauts-run-id"]
            conversation = streamed.headers["x-robinauts-conversation-id"]
            body = streamed.text
            (draining,) = drains
            # The stream ended with the hint to attach again, not with the turn's end.
            assert body.endswith(agui.reconnect_hint())
            assert "RUN_FINISHED" not in body
            assert "RUN_ERROR" not in body
            refused = await http.get("/ready")
            assert refused.status_code == 503
            assert refused.json()["problems"][0] == "draining"
            again = await http.post("/api/turns", json={"agent_id": "echo", "text": "hi"})
            assert again.status_code == 503
            events = f"/api/conversations/{conversation}/runs/{run_id}/events"
            assert (await http.get(events)).status_code == 503
            # The turn had its time and did not finish: it is interrupted, keeping its answer.
            await asyncio.wait_for(draining, 5.0)
            opened = (await http.get(f"/api/conversations/{conversation}")).json()
            assert opened["ended_badly"]["state"] == TurnState.INTERRUPTED.value
            assert opened["messages"][-1]["failed"] is True


@asyncio_test
async def test_a_turn_that_finishes_inside_the_drain_is_finished() -> None:
    config = dataclasses.replace(CONFIG, work=WorkConfig(drain_seconds=5.0))
    composed = compose(config, storage=StorageConfig(StorageKind.IN_MEMORY), secret_for={}.get)
    controller = composed.controller
    await controller.open()
    engine = GatedEngine()
    controller._engines["echo"] = engine  # type: ignore[attr-defined]
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="one")
    asyncio.get_running_loop().call_later(0.1, engine.gate.set)
    await composed.operations.drain()
    turn = await controller._store.get_turn(  # type: ignore[attr-defined]
        user.id, started.session_id, started.turn_id
    )
    assert turn is not None
    assert turn.state is TurnState.FINISHED
    readiness = await composed.operations.readiness()
    assert (readiness.ready, readiness.problems) == (False, ("draining",))
    await controller.close()
    assert json.dumps(readiness.problems)
