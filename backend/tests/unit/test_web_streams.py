# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Streams that a load balancer leaves open: keep-alives, and headers before a quiet turn's
first event; and a turn's deltas written in batches."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any

from test_controller_turns import CONFIG, opened, settled

from aio import asyncio_test
from robinauts.agent_engines.contract.domain import Done, Event, TextDelta
from robinauts.agent_engines.echo_engine.engine import EchoEngine
from robinauts.controller.composition import compose
from robinauts.controller.contract.domain import Identity, StorageConfig, StorageKind, TextPiece
from robinauts.controller.core.documents import event_from_document
from robinauts.web import agui, app


async def slowly(*chunks: str, pause: float) -> AsyncIterator[str]:
    for chunk in chunks:
        await asyncio.sleep(pause)
        yield chunk


@asyncio_test
async def test_a_quiet_stream_says_keep_alive_and_loses_nothing() -> None:
    sent = [c async for c in agui.kept_alive(slowly("a", "b", pause=0.25), 0.1)]
    assert [c for c in sent if c != agui.KEEP_ALIVE] == ["a", "b"]
    assert sent.count(agui.KEEP_ALIVE) >= 2
    assert agui.KEEP_ALIVE.startswith(":")


@asyncio_test
async def test_a_busy_stream_says_nothing_else() -> None:
    sent = [c async for c in agui.kept_alive(slowly("a", "b", "c", pause=0), 1.0)]
    assert sent == ["a", "b", "c"]


class QuietEngine(EchoEngine):
    """Says nothing until it is let go."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()

    async def stream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Event, None]:
        await self.gate.wait()
        async for event in super().stream(*args, **kwargs):
            yield event


async def asgi_stream(web: Any, method: str, path: str, body: bytes = b"", headers=()) -> Any:
    """A request to the app, and a queue of what it sends as it sends it: a stream, which
    httpx's ASGI transport would hold back until it ended."""
    sent: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    received = False

    async def receive() -> dict[str, Any]:
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"application/json"), *headers],
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
    }
    task = asyncio.create_task(web(scope, receive, sent.put))
    return sent, task


async def headers_of(sent: asyncio.Queue[dict[str, Any]]) -> dict[str, str]:
    start = await asyncio.wait_for(sent.get(), 5)
    assert start["type"] == "http.response.start"
    assert start["status"] == 200
    return {k.decode(): v.decode() for k, v in start["headers"]}


async def body_until(sent: asyncio.Queue[dict[str, Any]], done: Any) -> str:
    received = ""
    while not done(received):
        message = await asyncio.wait_for(sent.get(), 5)
        received += message.get("body", b"").decode()
        if not message.get("more_body", False):
            break
    return received


@asyncio_test
async def test_a_reattach_to_a_quiet_turn_answers_at_once_and_keeps_alive(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(app, "KEEP_ALIVE_SECONDS", 0.1)
    monkeypatch.setattr(app, "FIRST_EVENT_SECONDS", 0.05)
    composed = compose(CONFIG, storage=StorageConfig(StorageKind.IN_MEMORY), secret_for={}.get)
    web = app.create_app(
        composed.controller, credentials=composed.credentials, sign_in=None, secret_for={}.get
    )
    async with web.router.lifespan_context(web):
        engine = QuietEngine()
        composed.controller._engines["echo"] = engine  # type: ignore[attr-defined]
        posted, posting = await asgi_stream(
            web, "POST", "/api/turns", b'{"agent_id": "echo", "text": "hi"}'
        )
        started = await headers_of(posted)
        run, conversation = started["x-robinauts-run-id"], started["x-robinauts-conversation-id"]
        path = f"/api/conversations/{conversation}/runs/{run}/events"
        watched, watching = await asgi_stream(web, "GET", path, headers=[(b"last-event-id", b"1")])
        # Nothing new to say, and the headers came all the same, then keep-alives.
        await headers_of(watched)
        quiet = await body_until(watched, lambda got: got.count(agui.KEEP_ALIVE) >= 2)
        assert quiet.replace(agui.KEEP_ALIVE, "").startswith("event: RUN_STARTED")
        engine.gate.set()
        rest = await body_until(watched, lambda got: "RUN_FINISHED" in got)
        assert "TEXT_MESSAGE_CONTENT" in rest
        for task in (posting, watching):
            await asyncio.wait_for(task, 5)


class ChattyEngine(EchoEngine):
    """Many small pieces of text at once, then one after a pause."""

    async def stream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Event, None]:
        for n in range(50):
            yield TextDelta(f"{n} ")
        await asyncio.sleep(0.5)
        yield TextDelta("end")
        yield Done(text="", checkpoint_id=str(uuid.uuid4()))


@asyncio_test
async def test_deltas_are_merged_into_few_writes_and_none_is_lost() -> None:
    controller = await opened()
    controller._engines["echo"] = ChattyEngine()
    batches: list[int] = []
    store = controller._store
    appending = store.append_events

    async def counted(*args: Any, **kwargs: Any) -> None:
        batches.append(len(args[3]))
        await appending(*args, **kwargs)

    store.append_events = counted  # type: ignore[method-assign]
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="hi")
    await settled(controller, user, started)
    stored = await store.events_after(user.id, started.session_id, started.turn_id, 0)
    pieces = [
        e.event
        for e in (event_from_document(d) for _, d in stored)
        if isinstance(e.event, TextPiece)
    ]
    # The fifty pieces went out together, a moment later; the last on its own, at the end.
    assert [p.text for p in pieces] == ["".join(f"{n} " for n in range(50)), "end"]
    # The claim, then the merged fifty; the last piece goes with the finish.
    assert batches == [1, 1]
    answer = (await controller.open_session(user, started.session_id)).messages[-1]
    assert answer.parts[0].text == "".join(f"{n} " for n in range(50)) + "end"  # type: ignore[union-attr]
    await controller.close()
