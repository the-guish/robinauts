# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The turn runner: the engine's stream, stored as numbered turn events and an answer.

The runner numbers its turn's events from 1 and is their only writer. Its first append is
its claim on the turn: refused, it has lost the turn to another runner and runs no engine.
Its deadline is the turn's claim plus ``max_turn_seconds``; the process renews the lease. On
``TurnLostError`` from any write it closes the engine's stream and writes nothing more: the
turn is another runner's, a reader ended it, or its lease has passed.
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
    SessionNotFoundError,
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
from robinauts.controller.core.documents import (
    clean_text,
    event_to_document,
    message_from_document,
    stored_message,
)
from robinauts.controller.core.partial import failed_answer, with_text
from robinauts.controller.ports.store import Store, StoredEvent

CLOSE = "close"
"""The reason a closing worker cancels a task with. The runner ends such a turn as
``interrupted``, the deployment having stopped with the turn in it, not ``cancelled``."""

RETENTION = timedelta(hours=24)
"""How long a turn's events are kept after they are written, a constant for now."""

PAST_DEADLINE = "the turn ran past its deadline"
"""The error of a turn that did, the engine's ``TimeoutError`` having none to say."""

TIMED_OUT = "a call timed out before the turn's deadline"
"""The error of a turn whose silent ``TimeoutError`` came early, such as a query's."""


async def load_messages(store: Store, owner: uuid.UUID, session: uuid.UUID) -> list[Message]:
    """Every message of the session, decoded, oldest first."""
    return [message_from_document(d) for d in await store.messages_of(owner, session)]


async def end_if_running(
    store: Store,
    owner: uuid.UUID,
    session: uuid.UUID,
    turn: uuid.UUID,
    state: TurnState,
    now: datetime,
) -> None:
    """End the turn in that state, with no answer and no event, if it is still active and its
    task holds a lease: a runner that never claimed it wrote nothing. A turn lost, or a
    session gone, is left as it is."""
    try:
        await store.finish_turn(owner, session, turn, state, now, None, None, [], now)
    except (TurnLostError, SessionNotFoundError):
        return


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
    prompt: str,
    agent_config: AgentConfig,
    checkpoint_id: str | None,
    claimed_at: datetime,
    max_turn_seconds: float,
) -> None:
    # From the claim, not the start: a turn may wait queued for hours.
    deadline = claimed_at + timedelta(seconds=max_turn_seconds)
    remaining = (deadline - datetime.now(UTC)).total_seconds()
    if remaining <= 0:
        return
    answer_id = uuid.uuid4()
    definition = AgentDefinition(agent_config.system_prompt, agent_config.tools)
    parts: list[MessagePart] = []
    writer = _Writer(store, owner, turn)
    try:
        await writer.append(MessageStarted(answer_id, parent_id=question.id))
    except TurnLostError:
        return
    finishing: asyncio.Future[None] | None = None
    try:
        stream = engine.stream(
            session.id,
            definition,
            prompt,
            model=turn.model,
            checkpoint_id=checkpoint_id,
            timeout_seconds=remaining,
        )
        async with aclosing(stream) as events:
            async for event in events:
                if isinstance(event, TextDelta):
                    await writer.append(TextPiece(answer_id, event.text))
                    with_text(parts, event.text)
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
                    # An engine that streamed no text at all still hands the answer over.
                    if event.text and not any(isinstance(p, TextPart) for p in parts):
                        parts.append(TextPart(event.text))
                    answer = Message(
                        answer_id,
                        session.id,
                        parent_id=question.id,
                        role=Role.ASSISTANT,
                        parts=tuple(parts),
                        created_at=datetime.now(UTC),
                        agent=session.agent,
                        engine=session.engine,
                        model=turn.model,
                        checkpoint_id=event.checkpoint_id,
                        turn_id=turn.id,
                    )
                    last = writer.last(MessageCompleted(answer_id), TurnEnded(TurnState.FINISHED))
                    # Once the engine has handed the answer over, a cancellation must not
                    # lose it: the finish runs to its end whatever happens to this task.
                    finishing = asyncio.ensure_future(
                        writer.finish(TurnState.FINISHED, None, answer, last)
                    )
                    await asyncio.shield(finishing)
    except TurnLostError:
        return
    except asyncio.CancelledError as exc:
        if finishing is not None:
            await asyncio.wait({finishing})
            if not finishing.cancelled():
                finishing.exception()
        elif CLOSE in exc.args:
            # Like a crash: what it streamed is kept, as an answer marked failed.
            failed = failed_answer(answer_id, session, turn, parts, datetime.now(UTC))
            await _end(writer, TurnState.INTERRUPTED, None, failed)
        else:
            await _end(writer, TurnState.CANCELLED, None)
        raise
    except Exception as exc:
        failed = failed_answer(answer_id, session, turn, parts, datetime.now(UTC))
        error = clean_text(str(exc))
        if isinstance(exc, TimeoutError) and not error:
            # A query past the pool's time limit raises it too, early. The second of slack
            # is for the engine's clock, which is the loop's, not the wall's.
            late = datetime.now(UTC) >= deadline - timedelta(seconds=1)
            error = PAST_DEADLINE if late else TIMED_OUT
        await _end(writer, TurnState.FAILED, error, failed)


async def _end(
    writer: _Writer, state: TurnState, error: str | None, answer: Message | None = None
) -> None:
    """End a turn, with what it answered if anything; nothing more if the turn is lost."""
    try:
        await writer.finish(state, error, answer, writer.last(TurnEnded(state)))
    except (TurnLostError, SessionNotFoundError):
        return
