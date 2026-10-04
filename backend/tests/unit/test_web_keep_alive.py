# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A stream that goes quiet says a comment, so nothing in front of the deployment closes it."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
from test_controller_runner import GatedEngine
from test_controller_turns import CONFIG

from aio import asyncio_test
from robinauts.controller.composition import compose
from robinauts.controller.contract.domain import StorageConfig, StorageKind
from robinauts.web import agui
from robinauts.web.app import create_app


async def slow(*chunks: str, pause: float) -> AsyncIterator[str]:
    for chunk in chunks:
        await asyncio.sleep(pause)
        yield chunk


@asyncio_test
async def test_a_quiet_stream_says_keep_alive_and_then_what_comes() -> None:
    said = [c async for c in agui.kept_alive(slow("a", "b", pause=0.25), every=0.1)]
    assert said[-1] == "b"
    assert said.count(agui.KEEP_ALIVE) >= 2
    assert said.index("a") > said.index(agui.KEEP_ALIVE)
    assert [c for c in said if c != agui.KEEP_ALIVE] == ["a", "b"]


@asyncio_test
async def test_a_stream_that_never_goes_quiet_says_nothing_more() -> None:
    said = [c async for c in agui.kept_alive(slow("a", "b", "c", pause=0), every=0.1)]
    assert said == ["a", "b", "c"]


@asyncio_test
async def test_closing_the_stream_lets_go_of_what_it_waits_for() -> None:
    closed = asyncio.Event()

    async def endless() -> AsyncIterator[str]:
        try:
            yield "a"
            await asyncio.sleep(3600)
            yield "never"
        finally:
            closed.set()

    stream = agui.kept_alive(endless(), every=0.05)
    assert await anext(stream) == "a"
    assert await anext(stream) == agui.KEEP_ALIVE
    await stream.aclose()
    await asyncio.wait_for(closed.wait(), 1.0)


@asyncio_test
async def test_a_turn_streamed_over_the_wire_carries_keep_alives_while_it_is_quiet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agui, "KEEP_ALIVE_SECONDS", 0.05)
    composed = compose(CONFIG, storage=StorageConfig(StorageKind.IN_MEMORY), secret_for={}.get)
    app = create_app(
        composed.controller, credentials=composed.credentials, sign_in=None, secret_for={}.get
    )
    async with app.router.lifespan_context(app):
        engine = GatedEngine()
        composed.controller._engines["echo"] = engine  # type: ignore[attr-defined]
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            asyncio.get_running_loop().call_later(0.3, engine.gate.set)
            response = await http.post("/api/turns", json={"agent_id": "echo", "text": "hi"})
    body = response.text
    assert ": keep-alive\n\n" in body
    # The comments come while the turn waits, and its events after them, ending it.
    assert '"type":"RUN_FINISHED"' in body.rsplit(": keep-alive", 1)[-1]
