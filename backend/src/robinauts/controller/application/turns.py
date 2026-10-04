# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The turn runner: the engine's stream, stored as numbered turn events and an answer.

The runner numbers its turn's events from 1 and is their only writer, and every write names
the holder and the attempt it runs under (``Fence``). Its first append is its claim on the
turn: refused, it has lost the turn to another runner and runs no engine.
Its deadline is the turn's ``deadline_at``, kept short of the lease; each call to the
vendor is bounded by the model's own timeout, inside the engine. On
``TurnLostError`` from any write, or when it is stopped naming ``LOST``, it closes the
engine's stream and writes nothing more: the turn is another runner's, a reader ended it, or
its lease has passed.
"""

from __future__ import annotations

import asyncio
import contextlib
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
    BusyError,
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
from robinauts.controller.core.documents import (
    clean_text,
    event_to_document,
    stored_message,
)
from robinauts.controller.core.transcript import with_text
from robinauts.controller.ports.dispatcher import StopReason
from robinauts.controller.ports.store import Store, StoredEvent
from robinauts.controller.ports.work import Fence

log = logging.getLogger(__name__)

RETENTION = timedelta(hours=24)
"""How long a turn's events are kept after they are written, a constant for now."""

PROCESS_STOPPED = "the process stopped"
"""The error of a turn its process ended as it stopped, for the operator."""

FLUSH_SECONDS = 0.15
"""How long a piece of text or reasoning is held, at most, before it is written."""

BUSY_TRIES = 5
"""How often a batch is written while the database has no connection free in time."""

BUSY_BACKOFF = 0.5
"""Seconds before the next try of a batch, times the tries so far."""

DEADLINE_MARGIN = 10.0
"""Seconds of its deadline a turn must have left for the runner to claim it."""


class _Writer:
    """The turn's events, numbered in order and written in batches.

    The pieces of text and of reasoning are held for up to ``FLUSH_SECONDS``, consecutive ones
    of one message merged into one, and go out with the next other event, or on their own
    when the time is up. One batch is one write and one announcement to the watchers, so a
    model streaming a hundred tokens a second costs a handful of transactions, not a hundred.
    A batch written in the background that is refused is raised at the runner's next write.
    """

    def __init__(self, store: Store, owner: uuid.UUID, turn: Turn, fence: Fence) -> None:
        self._store = store
        self._owner = owner
        self._turn = turn
        self._fence = fence
        self.position = 0
        self._held: list[TurnEvent] = []
        self._lock = asyncio.Lock()
        self._timer: asyncio.TimerHandle | None = None
        self._flushing: asyncio.Task[None] | None = None
        self._failed: Exception | None = None

    def piece(self, event: TextPiece | ReasoningPiece) -> None:
        """Hold a piece, merged into the one before it when that is of the same kind and
        message, and see that it goes out within ``FLUSH_SECONDS``."""
        self._raise_failed()
        last = self._held[-1] if self._held else None
        if (
            isinstance(event, TextPiece)
            and isinstance(last, TextPiece)
            and last.message_id == event.message_id
        ):
            self._held[-1] = TextPiece(event.message_id, last.text + event.text)
        elif (
            isinstance(event, ReasoningPiece)
            and isinstance(last, ReasoningPiece)
            and last.message_id == event.message_id
        ):
            self._held[-1] = ReasoningPiece(event.message_id, last.text + event.text)
        else:
            self._held.append(event)
        self._schedule()

    async def append(self, *events: TurnEvent) -> None:
        """What is held, then these, as one batch. A batch the database had no connection
        for in time is written again; one that could not be written at all goes back to be
        held, unnumbered, so that the next write, or the finish, carries it and no position
        is lost."""
        self._raise_failed()
        async with self._lock:
            held = self._take() + list(events)
            if not held:
                return
            before = self.position
            batch = self._numbered(held)
            try:
                await _busy_retried(
                    lambda: self._store.append_events(
                        self._owner,
                        self._turn.session_id,
                        self._turn.id,
                        self._fence,
                        batch,
                        datetime.now(UTC),
                    )
                )
            except TurnLostError:
                raise
            except Exception:
                self.position = before
                self._held = held + self._held
                raise

    async def last(self, *events: TurnEvent) -> list[StoredEvent]:
        """What is held, then the events a finish writes, numbered after the ones written.
        ``TurnLostError`` when a batch written in the background was refused as lost."""
        async with self._lock:
            self._raise_failed()
            return self._numbered(self._take() + list(events))

    async def finish(
        self,
        state: TurnState,
        error: str | None,
        answer: Message | None,
        events: Sequence[StoredEvent],
    ) -> None:
        stored = None if answer is None else stored_message(answer)

        async def write() -> None:
            now = datetime.now(UTC)
            await self._store.finish_turn(
                self._owner,
                self._turn.session_id,
                self._turn.id,
                self._fence,
                state,
                now,
                error,
                stored,
                events,
                now,
            )

        # A finish the pool had no connection for wrote nothing, and is written again.
        await _busy_retried(write)

    async def close(self) -> None:
        """Write nothing more in the background."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        flushing, self._flushing = self._flushing, None
        if flushing is not None:
            flushing.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await flushing

    def _raise_failed(self) -> None:
        if self._failed is not None:
            raise self._failed

    def _schedule(self) -> None:
        if self._held and self._timer is None and self._flushing is None:
            loop = asyncio.get_running_loop()
            self._timer = loop.call_later(FLUSH_SECONDS, self._due)

    def _due(self) -> None:
        self._timer = None
        self._flushing = asyncio.create_task(self._flush())

    async def _flush(self) -> None:
        try:
            await self.append()
        except TurnLostError as lost:
            self._failed = lost
        except Exception:
            # Held again by `append`: the next write, or the finish, carries it.
            log.warning("a batch of the turn's events could not be written; it is held")
        finally:
            self._flushing = None
        if self._failed is None:
            self._schedule()

    def _take(self) -> list[TurnEvent]:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        held, self._held = self._held, []
        return held

    def _numbered(self, events: Sequence[TurnEvent]) -> list[StoredEvent]:
        stored = []
        expires_at = datetime.now(UTC) + RETENTION
        for event in events:
            self.position += 1
            document = event_to_document(self._turn.id, self.position, event)
            stored.append(StoredEvent(self.position, document, expires_at))
        return stored


async def run_turn(
    store: Store,
    engine: AgentEngine,
    owner: uuid.UUID,
    session: Session,
    turn: Turn,
    fence: Fence,
    question: Message,
    prompt: str,
    agent_config: AgentConfig,
    checkpoint_id: str | None,
) -> None:
    now = datetime.now(UTC)
    if turn.deadline_at is None or turn.lease_until <= now:
        return
    remaining = (turn.deadline_at - now).total_seconds()
    if remaining <= DEADLINE_MARGIN:
        return
    answer_id = uuid.uuid4()
    definition = AgentDefinition(agent_config.system_prompt, agent_config.tools)
    parts: list[MessagePart] = []
    writer = _Writer(store, owner, turn, fence)
    try:
        # The claim: alone, so that a runner refused writes nothing and runs no engine.
        await writer.append(MessageStarted(answer_id, parent_id=question.id))
    except TurnLostError:
        log.warning("the turn is held elsewhere: this runner writes nothing")
        return
    log.info("turn running, attempt %d, on %s", fence.attempt, turn.model)
    finishing: asyncio.Future[None] | None = None

    def failed() -> Message:
        """What it streamed before it stopped, as an answer marked failed: the thread shows
        it, a reply hangs under it, and Retry starts it over."""
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
        )
        async with aclosing(stream) as events:
            async for event in events:
                if isinstance(event, TextDelta):
                    writer.piece(TextPiece(answer_id, event.text))
                    with_text(parts, event.text)
                elif isinstance(event, ReasoningDelta):
                    writer.piece(ReasoningPiece(answer_id, event.text))
                elif isinstance(event, ToolCall):
                    arguments = json.dumps(dict(event.arguments))
                    await writer.append(
                        CallStarted(answer_id, event.call_id, event.name),
                        ArgumentsPiece(answer_id, event.call_id, arguments),
                        CallCompleted(answer_id, event.call_id),
                    )
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
                    last = await writer.last(
                        MessageCompleted(answer_id), TurnEnded(TurnState.FINISHED)
                    )
                    # Once the engine has handed the answer over, a cancellation must not
                    # lose it: the finish runs to its end whatever happens to this task.
                    finishing = asyncio.ensure_future(
                        writer.finish(TurnState.FINISHED, None, answer, last)
                    )
                    await asyncio.shield(finishing)
                    log.info("turn finished")
    except TurnLostError:
        log.warning("the turn was lost: ended elsewhere, or its lease passed")
        return
    except asyncio.CancelledError as exc:
        if finishing is not None:
            await asyncio.wait({finishing})
            if not finishing.cancelled():
                finishing.exception()
        elif StopReason.CLOSE in exc.args:
            log.info("turn interrupted: this process is stopping")
            await _end(writer, TurnState.INTERRUPTED, PROCESS_STOPPED, failed())
        elif StopReason.LOST not in exc.args:
            log.info("turn cancelled")
            await _end(writer, TurnState.CANCELLED, None)
        raise
    except Exception as exc:
        error = clean_text(str(exc))
        log.warning("turn failed: %s: %s", type(exc).__name__, error)
        await _end(writer, TurnState.FAILED, error, failed())
    finally:
        await writer.close()


async def _end(
    writer: _Writer, state: TurnState, error: str | None, answer: Message | None = None
) -> None:
    """End a turn, with what it answered if anything; nothing more if the turn is lost. An
    end the database would not take is logged and left: the turn's lease runs out, and
    whoever finds it then ends it, keeping what it had answered."""
    try:
        await writer.finish(state, error, answer, await writer.last(TurnEnded(state)))
    except TurnLostError:
        return
    except Exception:
        log.exception("the turn's end could not be written; it is left to its lease")


async def _busy_retried(write: Callable[[], Awaitable[None]]) -> None:
    """The write, tried again while the pool has no connection free in time: such a write
    did nothing, so trying it again is safe."""
    for tried in range(BUSY_TRIES):
        try:
            await write()
            return
        except BusyError:
            if tried == BUSY_TRIES - 1:
                raise
            await asyncio.sleep(BUSY_BACKOFF * (tried + 1))
