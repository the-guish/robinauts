# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The turn runner: the engine's stream, stored as numbered turn events and an answer.

The runner numbers its turn's events from 1 and is their only writer. Every write names the
process holding the turn and the attempt it holds, from the turn's record (``Fence``). Its
first append is its claim on the turn: refused, it has lost the turn to another runner and
runs no engine.
Its deadline is the turn's ``deadline_at``, which bounds the engine's run; each vendor call
has the model's own timeout, inside the engine. On
``TurnLostError`` from any write it closes the engine's stream and writes nothing more: the
turn is another runner's, a reader ended it, or its lease has passed; so it does when the
session was deleted under it. Stopped naming
``LOST``, by the heartbeat that found the same, it writes nothing more either.
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
    stored_message,
)
from robinauts.controller.ports.dispatcher import CLOSE, LOST
from robinauts.controller.ports.store import Fence, Store, StoredEvent

RETENTION = timedelta(hours=24)
"""How long a turn's events are kept after they are written, a constant for now."""

FLUSH_SECONDS = 0.15
"""How long a delta waits for the ones after it, so that they are written and announced
together."""

DEADLINE_MARGIN = 10.0
"""With less than this many seconds left before its deadline, the runner does not claim the
turn."""

LOST_WRITES = (TurnLostError, SessionNotFoundError)
"""What a refused write raises: the turn is not this runner's, or its session was deleted."""

DEADLINE_PASSED = "deadline passed"
"""The error of a turn the engine ended at its deadline."""


class _Writer:
    """The turn's events, numbered and written in batches.

    A delta (a piece of text, of reasoning or of a call's arguments) waits up to
    ``FLUSH_SECONDS``, merged with the deltas before it of the same message, and goes out
    with the next batch: one write and one announcement for many pieces. Any other event
    goes out at once, with the deltas before it. A batch that failed in the background is
    raised at the next append; the finish writes what is left whatever happened to it.
    """

    def __init__(self, store: Store, owner: uuid.UUID, turn: Turn) -> None:
        self._store = store
        self._owner = owner
        self._turn = turn
        self._fence = Fence(turn.worker_id or "", turn.attempt)
        self._pending: list[TurnEvent] = []
        self._lock = asyncio.Lock()
        self._timer: asyncio.Task[None] | None = None
        self._failed: BaseException | None = None
        self.position = 0

    async def append(self, event: TurnEvent) -> None:
        self._raise_failure()
        if isinstance(event, DELTAS):
            _merged(self._pending, event)
            if self._timer is None:
                self._timer = asyncio.create_task(self._flush_later())
            return
        self._pending.append(event)
        async with self._lock:
            self._stop_timer()
            events, self._pending = self._pending, []
            await self._write(events)

    async def _flush_later(self) -> None:
        await asyncio.sleep(FLUSH_SECONDS)
        # Before the lock, with no wait between: an append or a finish that finds no timer
        # takes the deltas itself.
        self._timer = None
        try:
            async with self._lock:
                events, self._pending = self._pending, []
                if events:
                    await self._write(events)
        except Exception as failed:
            self._failed = failed

    async def _write(self, events: list[TurnEvent]) -> None:
        now = datetime.now(UTC)
        await self._store.append_events(
            self._owner,
            self._turn.session_id,
            self._turn.id,
            self._numbered(events, now),
            now,
            fence=self._fence,
        )

    def _numbered(self, events: Sequence[TurnEvent], now: datetime) -> list[StoredEvent]:
        stored = []
        for event in events:
            self.position += 1
            document = event_to_document(self._turn.id, self.position, event)
            stored.append(StoredEvent(self.position, document, now + RETENTION))
        return stored

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _raise_failure(self) -> None:
        if self._failed is not None:
            failed, self._failed = self._failed, None
            raise failed

    def close(self) -> None:
        """Drop what is still waiting: the turn is not this runner's any more."""
        self._stop_timer()
        self._pending = []

    async def finish(
        self,
        state: TurnState,
        error: str | None,
        answer: Message | None,
        *last: TurnEvent,
    ) -> None:
        """The answer, with the deltas still waiting and ``last`` as the last events."""
        async with self._lock:
            self._stop_timer()
            # The end is the record that matters: a batch lost before it is not a reason to
            # lose it too.
            self._failed = None
            events, self._pending = [*self._pending, *last], []
            now = datetime.now(UTC)
            await self._store.finish_turn(
                self._owner,
                self._turn.session_id,
                self._turn.id,
                state,
                now,
                error,
                None if answer is None else stored_message(answer),
                self._numbered(events, now),
                now,
                fence=self._fence,
            )


DELTAS = (TextPiece, ReasoningPiece, ArgumentsPiece)
"""The events that arrive in many pieces, and are merged and written in batches."""


def _merged(pending: list[TurnEvent], event: TextPiece | ReasoningPiece | ArgumentsPiece) -> None:
    """A delta, onto the one before it when that is of the same kind and the same message, or
    the same call."""
    before = pending[-1] if pending else None
    if isinstance(event, ArgumentsPiece):
        if isinstance(before, ArgumentsPiece) and before.call_id == event.call_id:
            pending[-1] = ArgumentsPiece(event.message_id, event.call_id, before.text + event.text)
            return
    elif type(before) is type(event) and before.message_id == event.message_id:
        pending[-1] = type(event)(event.message_id, before.text + event.text)
        return
    pending.append(event)


def _with_text(parts: list[MessagePart], text: str) -> None:
    """Text that arrives in a row is one part, until a tool call comes between."""
    if parts and isinstance(parts[-1], TextPart):
        parts[-1] = TextPart(parts[-1].text + text)
    else:
        parts.append(TextPart(text))


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
    max_model_calls: int,
) -> None:
    deadline = turn.deadline_at or turn.lease_until
    remaining = (deadline - datetime.now(UTC)).total_seconds()
    if remaining <= DEADLINE_MARGIN:
        return
    answer_id = uuid.uuid4()
    definition = AgentDefinition(agent_config.system_prompt, agent_config.tools)
    parts: list[MessagePart] = []
    writer = _Writer(store, owner, turn)
    try:
        await writer.append(MessageStarted(answer_id, parent_id=question.id))
    except LOST_WRITES:
        return
    finishing: asyncio.Future[None] | None = None

    def failed() -> Message:
        return Message(
            answer_id,
            session.id,
            parent_id=question.id,
            role=Role.ASSISTANT,
            parts=tuple(parts),
            created_at=datetime.now(UTC),
            agent=session.agent,
            engine=session.engine,
            model=turn.model,
            turn_id=turn.id,
            failed=True,
        )

    try:
        stream = engine.stream(
            session.id,
            definition,
            prompt,
            model=turn.model,
            checkpoint_id=checkpoint_id,
            timeout_seconds=remaining,
            max_model_calls=max_model_calls,
        )
        async with aclosing(stream) as events:
            async for event in events:
                if isinstance(event, TextDelta):
                    await writer.append(TextPiece(answer_id, event.text))
                    _with_text(parts, event.text)
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
                    # Once the engine has handed the answer over, a cancellation must not
                    # lose it: the finish runs to its end whatever happens to this task.
                    finishing = asyncio.ensure_future(
                        writer.finish(
                            TurnState.FINISHED,
                            None,
                            answer,
                            MessageCompleted(answer_id),
                            TurnEnded(TurnState.FINISHED),
                        )
                    )
                    await asyncio.shield(finishing)
    except LOST_WRITES:
        writer.close()
        return
    except asyncio.CancelledError as exc:
        if finishing is not None:
            await asyncio.wait({finishing})
            if not finishing.cancelled():
                finishing.exception()
        elif CLOSE in exc.args:
            # The deployment stopped with the turn in it: what it did is kept, as when it
            # fails, so that Retry can start it again.
            await _end(writer, TurnState.INTERRUPTED, None, failed())
        elif LOST not in exc.args:
            await _end(writer, TurnState.CANCELLED, None)
        writer.close()
        raise
    except Exception as exc:
        # What it streamed before it failed is kept, as an answer marked failed: the
        # thread shows it, and a reply hangs under it.
        error = DEADLINE_PASSED if isinstance(exc, TimeoutError) else clean_text(str(exc))
        await _end(writer, TurnState.FAILED, error, failed())


async def _end(
    writer: _Writer, state: TurnState, error: str | None, answer: Message | None = None
) -> None:
    """End a turn, with what it answered if anything; nothing more if the turn is lost."""
    try:
        await writer.finish(state, error, answer, TurnEnded(state))
    except LOST_WRITES:
        return
