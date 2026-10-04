# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The turn runner: the engine's stream, stored as numbered turn events and an answer.

The runner numbers its turn's events from 1 and is their only writer. Every write names its
holder, the pod and the attempt, and its first append is its claim on the turn: refused, it
has lost the turn to another runner and runs no engine. Its deadline is the turn's
``deadline_at``; its lease is renewed by the pod's heartbeat meanwhile. On ``TurnLostError``
from any write, or when the heartbeat stops it as ``LOST``, it closes the engine's stream and
writes nothing more: the turn is another runner's, a reader ended it, or its lease has
passed.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
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
    MessageStarted,
    ReasoningPiece,
    ResultLanded,
    Role,
    Session,
    TextPart,
    TextPiece,
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
from robinauts.controller.core.transcript import parts_of
from robinauts.controller.ports.dispatcher import CLOSE, LOST
from robinauts.controller.ports.store import Holder, Store, StoredEvent

log = logging.getLogger(__name__)

RETENTION = timedelta(hours=24)
"""How long a turn's events are kept after they are written, a constant for now."""

FLUSH_SECONDS = 0.15
"""How long pieces of text wait to be written together."""

WRITE_TRIES = 4
"""How many times a write that found no free connection is tried, a second apart: a pool
busy for a moment does not fail a turn."""


async def _patiently[T](write: Callable[[], Awaitable[T]]) -> T:
    for tried in range(1, WRITE_TRIES + 1):
        try:
            return await write()
        except TimeoutError:
            if tried == WRITE_TRIES:
                raise
            await asyncio.sleep(1.0)
    raise AssertionError("unreachable")


class _Writer:
    """The turn's events, numbered and written in its holder's name, a batch at a time.

    Pieces of text and of reasoning wait up to ``FLUSH_SECONDS``, and a piece that follows
    another of its kind and message joins it, so a model streaming tokens costs a write and an
    announcement every so often rather than one per token. Any other event is written at once,
    with whatever waited before it, and a finish writes what waited in the same operation as
    the turn's end. A batch that a timer wrote and that was refused is raised by the next
    write.
    """

    def __init__(self, store: Store, owner: uuid.UUID, turn: Turn, holder: Holder) -> None:
        self._store = store
        self._owner = owner
        self._turn = turn
        self._holder = holder
        self.position = 0
        self._waiting: list[TurnEvent] = []
        self._writing = asyncio.Lock()
        self._timer: asyncio.Task[None] | None = None
        self._refused: BaseException | None = None

    async def append(self, event: TurnEvent) -> None:
        if self._refused is not None:
            raise self._refused
        if isinstance(event, TextPiece | ReasoningPiece):
            last = self._waiting[-1] if self._waiting else None
            if type(last) is type(event) and last.message_id == event.message_id:
                self._waiting[-1] = dataclasses.replace(last, text=last.text + event.text)
            else:
                self._waiting.append(event)
            if self._timer is None:
                self._timer = asyncio.create_task(self._later())
            return
        self._waiting.append(event)
        await self.flush()

    async def _later(self) -> None:
        await asyncio.sleep(FLUSH_SECONDS)
        self._timer = None
        try:
            await self.flush()
        except Exception as refused:
            self._refused = refused

    def _numbered(self, events: Sequence[TurnEvent]) -> list[StoredEvent]:
        stored = []
        expires = datetime.now(UTC) + RETENTION
        for event in events:
            self.position += 1
            document = event_to_document(self._turn.id, self.position, event)
            stored.append(StoredEvent(self.position, document, expires))
        return stored

    async def flush(self) -> None:
        """Write what waits, now, in one operation."""
        async with self._writing:
            if not self._waiting:
                return
            batch, self._waiting = self._waiting, []
            numbered = self._numbered(batch)
            await _patiently(
                lambda: self._store.append_events(
                    self._owner,
                    self._turn.session_id,
                    self._turn.id,
                    numbered,
                    datetime.now(UTC),
                    holder=self._holder,
                )
            )

    def drop(self) -> None:
        """Write nothing more: what waits is let go of, and the timer with it."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._waiting = []

    async def finish(
        self, state: TurnState, error: str | None, answer: Message | None, *last: TurnEvent
    ) -> None:
        """The turn's end, with what waited and ``last`` as its last events."""
        async with self._writing:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            batch, self._waiting = [*self._waiting, *last], []
            numbered = self._numbered(batch)
            stored = None if answer is None else stored_message(answer)

            async def finish() -> None:
                now = datetime.now(UTC)
                await self._store.finish_turn(
                    self._owner,
                    self._turn.session_id,
                    self._turn.id,
                    state,
                    now,
                    error,
                    stored,
                    numbered,
                    now,
                    holder=self._holder,
                )

            await _patiently(finish)


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
    holder: Holder,
) -> None:
    now = datetime.now(UTC)
    if turn.lease_until <= now or turn.deadline_at is None or turn.deadline_at <= now:
        return
    remaining = (turn.deadline_at - now).total_seconds()
    answer_id = uuid.uuid4()
    definition = AgentDefinition(agent_config.system_prompt, agent_config.tools)
    said: list[TurnEvent] = []
    writer = _Writer(store, owner, turn, holder)

    async def say(event: TurnEvent) -> None:
        await writer.append(event)
        said.append(event)

    def kept(failed: bool, checkpoint_id: str | None = None) -> Message:
        """The answer, of the parts what was said spells."""
        return Message(
            answer_id,
            session.id,
            parent_id=question.id,
            role=Role.ASSISTANT,
            parts=parts_of(said),
            created_at=datetime.now(UTC),
            agent=session.agent,
            engine=session.engine,
            model=turn.model,
            checkpoint_id=checkpoint_id,
            turn_id=turn.id,
            failed=failed,
        )

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
            max_model_calls=max_model_calls,
        )
        async with aclosing(stream) as events:
            async for event in events:
                if isinstance(event, TextDelta):
                    await say(TextPiece(answer_id, event.text))
                elif isinstance(event, ReasoningDelta):
                    await say(ReasoningPiece(answer_id, event.text))
                elif isinstance(event, ToolCall):
                    await say(CallStarted(answer_id, event.call_id, event.name))
                    arguments = json.dumps(dict(event.arguments))
                    await say(ArgumentsPiece(answer_id, event.call_id, arguments))
                    await say(CallCompleted(answer_id, event.call_id))
                elif isinstance(event, ToolResult):
                    await say(ResultLanded(answer_id, event.call_id, event.output, event.is_error))
                elif isinstance(event, Done):
                    answer = kept(False, event.checkpoint_id)
                    # An engine that streamed no text at all still hands the answer over.
                    if event.text and not any(isinstance(p, TextPart) for p in answer.parts):
                        answer = dataclasses.replace(
                            answer, parts=(*answer.parts, TextPart(event.text))
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
                    log.info("turn ended as finished")
    except TurnLostError:
        log.warning("the turn is no longer this pod's: its runner stops")
        return
    except asyncio.CancelledError as exc:
        if finishing is not None:
            await asyncio.wait({finishing})
            if not finishing.cancelled():
                finishing.exception()
        elif LOST in exc.args:
            pass  # the turn is not this pod's any more: nothing is written in its name
        elif CLOSE in exc.args:
            # Stopped by the deployment, not by anyone: what it did is kept, as an answer
            # marked failed, which the thread shows and Retry starts over from.
            await _end(writer, TurnState.INTERRUPTED, None, kept(True))
        else:
            await _end(writer, TurnState.CANCELLED, None)
        raise
    except Exception as exc:
        # What it streamed before it failed is kept, as an answer marked failed: the
        # thread shows it, and a reply hangs under it.
        await _end(writer, TurnState.FAILED, clean_text(str(exc)), kept(True))
    finally:
        writer.drop()


async def _end(
    writer: _Writer, state: TurnState, error: str | None, answer: Message | None = None
) -> None:
    """End a turn, with what it answered if anything; nothing more if the turn is lost."""
    try:
        await writer.finish(state, error, answer, TurnEnded(state))
    except TurnLostError:
        log.warning("the turn was lost before it could end as %s", state.value)
        return
    log.info("turn ended as %s", state.value)
