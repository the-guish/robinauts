# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The wiring the run lifecycle's tests share: the service over every fake.

``application.Turns`` with an in-memory store, a clock that stands still, ids
a test knows in advance and an engine a test writes the script for. Nothing
here decides anything -- it is the ports and the services, built the way a
deployment builds them -- and the readers at the bottom are what a test looks
at afterwards: the messages that were stored, the events that were published,
and whether the stream reads back.

**The executor and the signals are the real adapters**, not fakes. They are
in-memory and touch nothing outside the process -- tasks on the test's own
loop, and futures -- so a fake of either would be a second implementation of
the same few lines, and a test of the lifecycle that did not use the real one
would prove less. ``submitted`` is how a test hands a turn over the way a
request does (claim, then submit), which is what makes ``cancel`` reach it,
and ``settled`` waits for the work to let go.

``readable`` is the one that matters. **Everything ``execute`` stores must
satisfy ``core.check_event_order``**, in every scenario -- finished, failed,
cancelled, timed out, swept -- so every test of a lifecycle ends by asking it,
over the documents as they are stored rather than over what the service
returned.
"""

from __future__ import annotations

import asyncio
import functools
import uuid
from dataclasses import dataclass

from conversations import AGENT, OTHER_MODEL, OWNER, agent_definition, at, offered
from fakes import CountingIdSource, FakeClock, MemoryConversationStore, ScriptedAgent, Step
from robinauts.adapters import AsyncioRunExecutor, MemoryRunSignals
from robinauts.application import Conversations, Turns, Watch
from robinauts.core import check_event_order, message_from_stored, run_event_from_stored
from robinauts.domain import AgentDefinition, Message, ModelConfig, Run, RunEvent, User
from robinauts.ports import ConversationStore

NOW = at(100)
"""What the clock says while a test runs, unless the test moves it."""

STRANGER = uuid.UUID("77777777-7777-4777-8777-777777777777")
"""Somebody else, who owns nothing here and to whom nothing here belongs."""

AUTHOR = User(id=OWNER, provider="google", subject="1", name="Ada", email=None, created_at=at(0))
SOMEBODY_ELSE = User(
    id=STRANGER, provider="google", subject="2", name="Bob", email=None, created_at=at(0)
)


@dataclass(frozen=True, slots=True)
class Wiring:
    """The services, the ports under them, and the store a test looks in."""

    turns: Turns
    conversations: Conversations
    watch: Watch
    executor: AsyncioRunExecutor
    signals: MemoryRunSignals
    store: MemoryConversationStore
    clock: FakeClock
    ids: CountingIdSource
    agent: ScriptedAgent
    definition: AgentDefinition


def wired(
    *steps: Step,
    definition: AgentDefinition | None = None,
    models: dict[str, ModelConfig] | None = None,
    store: MemoryConversationStore | None = None,
    signals: MemoryRunSignals | None = None,
    turn_seconds: float = 30.0,
    wait_seconds: float = 30.0,
    quiet_seconds: float = 300.0,
) -> Wiring:
    """``Turns`` over the fakes, with an engine that runs that script.

    It offers ``MODEL``, which the agent starts on, and ``OTHER_MODEL`` to move
    a conversation to, unless the test names its own ``models``.
    """
    kept = definition if definition is not None else agent_definition()
    store = store if store is not None else MemoryConversationStore()
    clock = FakeClock(now=NOW)
    ids = CountingIdSource()
    agent = ScriptedAgent(*steps)
    executor = AsyncioRunExecutor()
    signals = signals if signals is not None else MemoryRunSignals()
    return Wiring(
        turns=Turns(
            store=store,
            clock=clock,
            ids=ids,
            agents={kept.id: kept},
            models=models if models is not None else offered(OTHER_MODEL),
            engines={kept.engine: agent},
            executor=executor,
            signals=signals,
            turn_seconds=turn_seconds,
        ),
        conversations=Conversations(store=store, clock=clock),
        watch=Watch(
            store=store,
            signals=signals,
            wait_seconds=wait_seconds,
            quiet_seconds=quiet_seconds,
        ),
        executor=executor,
        signals=signals,
        store=store,
        clock=clock,
        ids=ids,
        agent=agent,
        definition=kept,
    )


def submitted(wiring: Wiring, run: Run) -> None:
    """Hand a run's work to the executor, the way a request does.

    ``Turns.begin`` is this after ``start``; a test that began its turn some
    other way -- or that wants the run before the work exists -- says it here.
    Claimed first and submitted second, which is the order that leaves no
    instant in which nothing knows the run is being answered, and with the
    same **never-began report** the service passes, because the instant
    before the first step is the one thing that report is for.
    """
    wiring.turns.claim(run.id)
    wiring.executor.submit(
        run.id,
        functools.partial(wiring.turns.execute, run),
        never_began=functools.partial(wiring.turns._never_began, run),
    )


async def settled(wiring: Wiring, run: Run) -> None:
    """Wait until the executor has let go of that run's work.

    What a test awaits instead of a task it created itself, now that the task
    is the executor's. Nothing sleeps: it yields to the loop, and the work is
    what runs.
    """
    while run.id in wiring.turns.executing:
        await asyncio.sleep(0)


async def begun(wiring: Wiring, text: str = "What is a robinaut?") -> Run:
    """A new conversation with one question in it, and the run answering it."""
    return (await wiring.turns.start(AUTHOR, agent_id=AGENT, text=text)).run


async def stored_messages(store: ConversationStore, conversation_id: uuid.UUID) -> list[Message]:
    """The messages of that conversation, as they are stored, oldest first."""
    return [message_from_stored(document) for document in await store.messages_of(conversation_id)]


async def stored_events(
    store: ConversationStore, run_id: uuid.UUID, after: int = 0
) -> list[RunEvent]:
    """That run's events, as they are stored, in order of position."""
    return [
        run_event_from_stored(document) for document in await store.events_of(run_id, after=after)
    ]


async def written(store: ConversationStore, run_id: uuid.UUID, position: int) -> None:
    """Wait until that run's stream has reached ``position``.

    **The checkpoint a test needs, now that the engine is read by a task of
    its own.** A gate says where the *engine* is; it says nothing about how
    much of what it yielded the lifecycle has written, because the events
    ahead of the writer sit in a queue. So a test that is about what is stored
    waits for what is stored. Nothing sleeps: it yields to the loop, and the
    writer is the only thing that can run.
    """
    while await store.last_position(run_id) < position:
        await asyncio.sleep(0)


def readable(events: list[RunEvent], run: Run, *, ended: bool = True) -> None:
    """The whole of a run's stored stream, read back; raises if it does not.

    The three things ``core.check_event_order`` is not allowed to guess are
    the three a run knows: which run, which conversation, and what the first
    message of the stream hangs under -- the question the run answers.
    """
    check_event_order(
        events,
        run_id=run.id,
        conversation_id=run.conversation_id,
        follows=run.message_id,
        ended=ended,
    )


def kinds(events: list[RunEvent]) -> list[str]:
    """The kinds of the events of a stream, in order: what a test reads at a glance."""
    return [type(event.event).__name__ for event in events]
