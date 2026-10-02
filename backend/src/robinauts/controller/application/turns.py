# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The turn runner: the engine's stream, stored as numbered turn events and an answer.

The runner numbers its turn's events from 1 and is their only writer. Its first append is
its claim on the turn: refused, it has lost the turn to another runner and runs no engine.
On ``TurnLostError`` from any write it closes the engine's stream and writes nothing more:
the turn is another runner's, a reader ended it, or its lease has passed.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Sequence
from contextlib import aclosing
from datetime import UTC, datetime, timedelta

from robinauts.agent_engines.contract.domain import (
    AgentDefinition,
    Done,
    ReasoningDelta,
    TextDelta,
    ToolCall,
    ToolResult,
)
from robinauts.agent_engines.contract.ports import AgentEngine
from robinauts.controller.application.documents import (
    clean_text,
    event_to_document,
    stored_message,
)
from robinauts.controller.contract.domain import (
    AgentConfig,
    ArgumentsPiece,
    CallCompleted,
    CallStarted,
    Message,
    MessageCompleted,
    MessagePart,
    MessageStarted,
    ReasoningPiece,
    ResultLanded,
    Role,
    Session,
    TextPart,
    TextPiece,
    ToolCallPart,
    ToolResultPart,
    Turn,
    TurnEnded,
    TurnEvent,
    TurnLostError,
    TurnState,
)
from robinauts.controller.ports.store import Store, StoredEvent

RETENTION = timedelta(hours=24)
"""How long a turn's events are kept after they are written, a constant for now."""


class _Writer:
    """The turn's events, numbered and written one at a time."""

    def __init__(self, store: Store, owner: uuid.UUID, turn: Turn) -> None:
        self._store = store
        self._owner = owner
        self._turn = turn
        self.position = 0

    async def append(self, event: TurnEvent) -> None:
        self.position += 1
        now = datetime.now(UTC)
        await self._store.append_event(
            self._owner,
            self._turn.session_id,
            self._turn.id,
            self.position,
            event_to_document(self._turn.id, self.position, event),
            now,
            now + RETENTION,
        )

    def last(self, *events: TurnEvent) -> list[StoredEvent]:
        """The events a finish writes, numbered after the ones appended."""
        stored = []
        now = datetime.now(UTC)
        for event in events:
            self.position += 1
            document = event_to_document(self._turn.id, self.position, event)
            stored.append(StoredEvent(self.position, document, now + RETENTION))
        return stored

    async def finish(
        self,
        state: TurnState,
        error: str | None,
        answer: Message | None,
        events: Sequence[StoredEvent],
    ) -> None:
        now = datetime.now(UTC)
        await self._store.finish_turn(
            self._owner,
            self._turn.session_id,
            self._turn.id,
            state,
            now,
            error,
            None if answer is None else stored_message(answer),
            events,
            now,
        )


async def run_turn(
    store: Store,
    engine: AgentEngine,
    owner: uuid.UUID,
    session: Session,
    turn: Turn,
    question: Message,
    agent_config: AgentConfig,
    checkpoint_id: str | None,
    timeout_seconds: float,
) -> None:
    answer_id = uuid.uuid4()
    prompt = "".join(p.text for p in question.parts if isinstance(p, TextPart))
    definition = AgentDefinition(agent_config.system_prompt, agent_config.tools)
    parts: list[MessagePart] = []
    writer = _Writer(store, owner, turn)
    try:
        await writer.append(MessageStarted(answer_id, parent_id=question.id))
    except TurnLostError:
        return
    try:
        stream = engine.stream(
            session.id,
            definition,
            prompt,
            model=turn.model,
            checkpoint_id=checkpoint_id,
            timeout_seconds=timeout_seconds,
        )
        async with aclosing(stream) as events:
            async for event in events:
                if isinstance(event, TextDelta):
                    await writer.append(TextPiece(answer_id, event.text))
                elif isinstance(event, ReasoningDelta):
                    await writer.append(ReasoningPiece(answer_id, event.text))
                elif isinstance(event, ToolCall):
                    await writer.append(CallStarted(answer_id, event.call_id, event.name))
                    arguments = json.dumps(dict(event.arguments))
                    await writer.append(ArgumentsPiece(answer_id, event.call_id, arguments))
                    await writer.append(CallCompleted(answer_id, event.call_id))
                    parts.append(ToolCallPart(event.call_id, event.name, event.arguments))
                elif isinstance(event, ToolResult):
                    await writer.append(
                        ResultLanded(answer_id, event.call_id, event.output, event.is_error)
                    )
                    parts.append(ToolResultPart(event.call_id, event.output, event.is_error))
                elif isinstance(event, Done):
                    answer = Message(
                        answer_id,
                        session.id,
                        parent_id=question.id,
                        role=Role.ASSISTANT,
                        parts=(*parts, TextPart(event.text)),
                        created_at=datetime.now(UTC),
                        agent=session.agent,
                        engine=session.engine,
                        model=turn.model,
                        checkpoint_id=event.checkpoint_id,
                        turn_id=turn.id,
                    )
                    await writer.finish(
                        TurnState.FINISHED,
                        None,
                        answer,
                        writer.last(MessageCompleted(answer_id), TurnEnded(TurnState.FINISHED)),
                    )
    except TurnLostError:
        return
    except asyncio.CancelledError:
        await _end(writer, TurnState.CANCELLED, None)
        raise
    except Exception as exc:
        await _end(writer, TurnState.FAILED, clean_text(str(exc)))


async def _end(writer: _Writer, state: TurnState, error: str | None) -> None:
    """End a turn that produced no answer; nothing more if the turn is already lost."""
    try:
        await writer.finish(state, error, None, writer.last(TurnEnded(state)))
    except TurnLostError:
        return
