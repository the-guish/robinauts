# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The controller's turn events as AG-UI events over server-sent events (``docs/specs/wire.md``).

The run id is the turn's id, and the thread id the session's: each turn is a run of its own.
A stream that has said nothing for ``KEEP_ALIVE_SECONDS`` says an SSE comment, so that a load
balancer or a proxy does not close it as idle while a tool runs. A stream of a process that is
stopping ends with SSE's ``retry:`` instead of the turn's end, so its client attaches again,
to another process.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from ag_ui.core import (
    BaseEvent,
    ReasoningMessageContentEvent,
    ReasoningMessageEndEvent,
    ReasoningMessageStartEvent,
    RunErrorEvent,
    RunFinishedCancelledOutcome,
    RunFinishedEvent,
    RunStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)
from ag_ui.encoder import EventEncoder

from robinauts.controller.contract.domain import (
    ArgumentsPiece,
    CallCompleted,
    CallStarted,
    MessageCompleted,
    MessageStarted,
    NumberedEvent,
    ReasoningPiece,
    ResultLanded,
    Role,
    TextPiece,
    TurnEnded,
    TurnEvent,
    TurnState,
)

ENCODER = EventEncoder()

KEEP_ALIVE_SECONDS = 15.0
KEEP_ALIVE = ": keep-alive\n\n"
"""An SSE comment: bytes on the connection, and nothing a client reads as an event."""

RECONNECT = "retry: 1000\n\n"
"""How a stream of a stopping process ends: attach again, in a second, elsewhere."""

ENDED_BADLY = {
    TurnState.FAILED: "the agent could not finish this answer",
    TurnState.INTERRUPTED: "the deployment stopped while this answer was being produced",
}


def sse(event: BaseEvent, position: int | None = None) -> str:
    numbered = "" if position is None else f"id: {position}\n"
    return f"{numbered}event: {event.type.value}\n{ENCODER.encode(event)}"


def mapped(thread_id: str, run_id: str, event: TurnEvent) -> list[BaseEvent]:
    match event:
        case MessageStarted(role=Role.ASSISTANT):
            return [TextMessageStartEvent(message_id=str(event.message_id), role="assistant")]
        case TextPiece():
            return [TextMessageContentEvent(message_id=str(event.message_id), delta=event.text)]
        case CallStarted():
            return [
                ToolCallStartEvent(
                    tool_call_id=event.call_id,
                    tool_call_name=event.name,
                    parent_message_id=str(event.message_id),
                )
            ]
        case ArgumentsPiece():
            return [ToolCallArgsEvent(tool_call_id=event.call_id, delta=event.text)]
        case CallCompleted():
            return [ToolCallEndEvent(tool_call_id=event.call_id)]
        case ResultLanded():
            return [
                ToolCallResultEvent(
                    message_id=str(event.message_id),
                    tool_call_id=event.call_id,
                    content=event.text,
                    role="tool",
                    metadata={"isError": True} if event.is_error else None,
                )
            ]
        case MessageCompleted():
            return [TextMessageEndEvent(message_id=str(event.message_id))]
        case TurnEnded(state=TurnState.FINISHED):
            return [RunFinishedEvent(thread_id=thread_id, run_id=run_id)]
        case TurnEnded(state=TurnState.CANCELLED):
            return [
                RunFinishedEvent(
                    thread_id=thread_id, run_id=run_id, outcome=RunFinishedCancelledOutcome()
                )
            ]
        case TurnEnded():
            return [RunErrorEvent(message=ENDED_BADLY[event.state], code=event.state.value)]
    return []


async def stream(
    thread_id: str, run_id: str, events: AsyncIterator[NumberedEvent]
) -> AsyncIterator[str]:
    """``RUN_STARTED``, then each event; the position goes on the last wire event of each."""
    yield sse(RunStartedEvent(thread_id=thread_id, run_id=run_id))
    thinking: str | None = None
    async for numbered in events:
        event, position = numbered.event, numbered.position
        if isinstance(event, ReasoningPiece):
            if thinking is None:
                thinking = f"{event.message_id}:reasoning:{position}"
                yield sse(ReasoningMessageStartEvent(message_id=thinking))
            yield sse(ReasoningMessageContentEvent(message_id=thinking, delta=event.text), position)
            continue
        if thinking is not None:
            yield sse(ReasoningMessageEndEvent(message_id=thinking))
            thinking = None
        wire = mapped(thread_id, run_id, event)
        for index, sent in enumerate(wire):
            yield sse(sent, position if index == len(wire) - 1 else None)


async def kept_alive(
    chunks: AsyncIterator[str],
    stopping: asyncio.Event | None = None,
    every: float = KEEP_ALIVE_SECONDS,
) -> AsyncIterator[str]:
    """``chunks``, with ``KEEP_ALIVE`` after every ``every`` seconds in which none came, until
    ``stopping`` is set: then ``RECONNECT``, and the end."""
    stopped = asyncio.ensure_future((stopping or asyncio.Event()).wait())
    waiting: asyncio.Task[str] | None = None
    try:
        while True:
            waiting = waiting or asyncio.ensure_future(anext(chunks))
            await asyncio.wait({waiting, stopped}, timeout=every, return_when="FIRST_COMPLETED")
            if not waiting.done():
                yield RECONNECT if stopped.done() else KEEP_ALIVE
                if stopped.done():
                    return
                continue
            try:
                chunk = waiting.result()
            except StopAsyncIteration:
                return
            finally:
                waiting = None
            yield chunk
    finally:
        for pending in (stopped, waiting):
            if pending is not None:
                pending.cancel()
                with contextlib.suppress(BaseException):
                    await pending
