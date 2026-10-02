# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The controller's turn events as AG-UI events over server-sent events (``docs/specs/wire.md``).

The run id is the session's id: a session has one active turn at a time.
"""

from __future__ import annotations

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

ENDED_BADLY = {
    TurnState.FAILED: "the agent could not finish this answer",
    TurnState.INTERRUPTED: "the deployment stopped while this answer was being produced",
}


def sse(event: BaseEvent, position: int | None = None) -> str:
    numbered = "" if position is None else f"id: {position}\n"
    return f"{numbered}event: {event.type.value}\n{ENCODER.encode(event)}"


def mapped(run_id: str, event: TurnEvent) -> tuple[BaseEvent, ...]:
    match event:
        case MessageStarted(role=Role.ASSISTANT):
            return (TextMessageStartEvent(message_id=str(event.message_id), role="assistant"),)
        case TextPiece():
            return (TextMessageContentEvent(message_id=str(event.message_id), delta=event.text),)
        case CallStarted():
            return (
                ToolCallStartEvent(
                    tool_call_id=event.call_id,
                    tool_call_name=event.name,
                    parent_message_id=str(event.message_id),
                ),
            )
        case ArgumentsPiece():
            return (ToolCallArgsEvent(tool_call_id=event.call_id, delta=event.text),)
        case CallCompleted():
            return (ToolCallEndEvent(tool_call_id=event.call_id),)
        case ResultLanded():
            return (
                ToolCallResultEvent(
                    message_id=str(event.message_id),
                    tool_call_id=event.call_id,
                    content=event.text,
                    role="tool",
                    metadata={"isError": True} if event.is_error else None,
                ),
            )
        case MessageCompleted():
            return (TextMessageEndEvent(message_id=str(event.message_id)),)
        case TurnEnded(state=TurnState.FINISHED):
            return (RunFinishedEvent(thread_id=run_id, run_id=run_id),)
        case TurnEnded(state=TurnState.CANCELLED):
            return (
                RunFinishedEvent(
                    thread_id=run_id, run_id=run_id, outcome=RunFinishedCancelledOutcome()
                ),
            )
        case TurnEnded():
            return (RunErrorEvent(message=ENDED_BADLY[event.state], code=event.state.value),)
    return ()


async def stream(run_id: str, events: AsyncIterator[NumberedEvent]) -> AsyncIterator[str]:
    """``RUN_STARTED``, then each event; the position goes on the last wire event of each."""
    yield sse(RunStartedEvent(thread_id=run_id, run_id=run_id))
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
        wire = mapped(run_id, event)
        for index, sent in enumerate(wire):
            yield sse(sent, position if index == len(wire) - 1 else None)
