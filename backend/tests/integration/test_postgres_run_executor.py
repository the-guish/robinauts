# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A run in the background, watched, against the real PostgreSQL.

The pieces of this step over the store they will run over: ``Turns.begin``
hands the work of a run to the ``AsyncioRunExecutor``, the work writes the
turn into the database as it is produced, ``MemoryRunSignals`` says so after
every write, and ``Watch`` reads it back out for the people watching.

What only a database can be asked here is whether any of it holds when the
writes are real ones: a watcher that attached before the turn began and one
that attached in the middle of it -- where the conversation told it to -- must
each receive their slice, in order, once, numbered on from where they asked,
and be told the run has ended. The unit tests prove the rules; this proves
they survive a store that really awaits.

Marked ``io`` and ``database``; skipped, with the reason, without
``ROBINAUTS_TEST_DATABASE_URL``.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

from aio import asyncio_test
from conversations import AGENT, agent_definition, at, offered
from fakes import CountingIdSource, FakeClock, Gate, ScriptedAgent, calls, results, says
from postgres import requires_postgres, temporary_schema
from robinauts.legacy.adapters import AsyncioRunExecutor, MemoryRunSignals
from robinauts.legacy.application import Conversations, Turns, Watch
from robinauts.legacy.core import check_event_order, message_from_stored, run_event_from_stored
from robinauts.legacy.datastore import PostgresConversationStore, PostgresCredentialStore
from robinauts.legacy.domain import Role, RunEnded, RunEvent, RunState, User
from robinauts.legacy.ports import ConversationStore

pytestmark = requires_postgres

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)

FIRST = "Someone who plays fair."
SECOND = "And that is all."

LOOKED = ("toolu_01", "search", {"q": "robinauts"})
"""A call the first answer makes, as ``calls`` wants it.

What puts two answers in one turn now that the framework runs the loop: the
turn ends once, with ``Done``, so a turn held half-way is held between an
answer that called a tool and the answer that follows the result.
"""

FOUND = ("toolu_01", "search", "found 3")
"""What the tool came back with, as ``results`` wants it."""

STEP = 0.005
"""How long a test waits between two looks at the database.

Not a timing assumption: what is waited for always happens, and this is only
how often a real store is asked about it.
"""


class Wired:
    """The services of this step over one schema's pool."""

    def __init__(self, store: ConversationStore, *steps: object) -> None:
        definition = agent_definition()
        clock = FakeClock(now=at(100))
        self.store = store
        self.executor = AsyncioRunExecutor()
        self.signals = MemoryRunSignals()
        self.turns = Turns(
            store=store,
            clock=clock,
            ids=CountingIdSource(),
            agents={definition.id: definition},
            models=offered(),
            engines={definition.engine: ScriptedAgent(*steps)},  # type: ignore[arg-type]
            executor=self.executor,
            signals=self.signals,
        )
        self.conversations = Conversations(store=store, clock=clock)
        self.watch = Watch(store=store, signals=self.signals, wait_seconds=5.0)


def watching(
    wired: Wired, user: User, run_id: uuid.UUID, *, after: int = 0
) -> tuple[asyncio.Task[None], list[RunEvent]]:
    """Somebody watching that run from ``after``, and what they receive."""
    seen: list[RunEvent] = []
    events = wired.watch.events(user, run_id, after=after)

    async def collect() -> None:
        async for event in events:
            seen.append(event)

    return asyncio.get_running_loop().create_task(collect()), seen


async def reaches(store: ConversationStore, run_id: uuid.UUID, position: int) -> None:
    """Wait until that run's stream is stored as far as ``position``."""
    while await store.last_position(run_id) < position:
        await asyncio.sleep(STEP)


@asyncio_test
async def test_a_turn_runs_in_the_background_and_two_watchers_are_served() -> None:
    async with temporary_schema() as schema:
        credentials = PostgresCredentialStore(schema.pool)
        store = PostgresConversationStore(schema.pool)
        author = await credentials.user_at_sign_in("google", "1", name="Ada", email=None, now=NOW)
        between = Gate()
        wired = Wired(store, *calls(LOOKED, text=FIRST), *results(FOUND), between, *says(SECOND))

        # A request: the turn is begun and answered at once, and the work goes
        # on in this process while nothing holds on to it.
        started = await wired.turns.begin(author, agent_id=AGENT, text="What is a robinaut?")
        run = started.run
        assert run.state is RunState.RUNNING
        from_the_start, whole = watching(wired, author, run.id)

        # The first answer, with its call, and the tool message that answers
        # it are in the conversation (positions 1 to 10); the turn is held
        # before the second answer. This is the moment somebody reloads the
        # page.
        await between.reached.wait()
        await reaches(store, run.id, 10)
        opened = await wired.conversations.open(author, run.conversation_id)
        assert opened.run_id == run.id
        assert opened.resume is not None
        from_the_middle, rest = watching(wired, author, run.id, after=opened.resume.after)
        between.open()

        await asyncio.gather(from_the_start, from_the_middle)
        await wired.executor.aclose(timeout=10.0)

        # The whole run, as one watcher had it and as it is stored.
        stored = [run_event_from_stored(document) for document in await store.events_of(run.id)]
        assert whole == stored
        check_event_order(
            whole,
            run_id=run.id,
            conversation_id=run.conversation_id,
            follows=run.message_id,
            ended=True,
        )
        assert isinstance(whole[-1].event, RunEnded)
        # And the slice the second one was sent: numbered on from where the
        # conversation said, hanging under the message it said, once.
        check_event_order(
            rest,
            run_id=run.id,
            conversation_id=run.conversation_id,
            follows=opened.resume.follows,
            after=opened.resume.after,
            ended=True,
        )
        assert rest[0].seq == opened.resume.after + 1
        assert rest == stored[opened.resume.after :]

        # And a watcher that attaches once it is over finishes, wherever it
        # asks from -- the end of it, and past the end of it.
        last = await store.last_position(run.id)
        for after in (last - 1, last, last + 1):
            late, told = watching(wired, author, run.id, after=after)
            await asyncio.wait_for(late, timeout=5.0)
            assert [event.seq for event in told] == list(range(after + 1, last + 1))

        ended = await store.run_by_id(run.id)
        assert ended is not None
        assert ended.state is RunState.FINISHED
        answers = [
            message_from_stored(document)
            for document in await store.messages_of(run.conversation_id)
        ]
        assert [(message.role, message.text) for message in answers] == [
            (Role.USER, "What is a robinaut?"),
            (Role.ASSISTANT, FIRST),
            (Role.TOOL, ""),
            (Role.ASSISTANT, SECOND),
        ]
        assert wired.turns.executing == frozenset()


@asyncio_test
async def test_a_process_that_stops_interrupts_the_run_it_was_answering() -> None:
    # The shutdown of a deployment, against the store that has to record it:
    # the work is cancelled, the run ends `interrupted` with the event that
    # says so, and the watcher is told rather than left waiting.
    async with temporary_schema() as schema:
        credentials = PostgresCredentialStore(schema.pool)
        store = PostgresConversationStore(schema.pool)
        author = await credentials.user_at_sign_in("google", "1", name="Ada", email=None, now=NOW)
        held = Gate()
        wired = Wired(store, *calls(LOOKED, text=FIRST), *results(FOUND), held, *says(SECOND))

        started = await wired.turns.begin(author, agent_id=AGENT, text="What is a robinaut?")
        watcher, seen = watching(wired, author, started.run.id)
        await held.reached.wait()
        await reaches(store, started.run.id, 10)

        wired.turns.stopping()
        await wired.executor.aclose(timeout=10.0)

        await watcher
        ended = await store.run_by_id(started.run.id)
        assert ended is not None
        assert ended.state is RunState.INTERRUPTED
        assert isinstance(seen[-1].event, RunEnded)
        assert seen[-1].event.state is RunState.INTERRUPTED
        check_event_order(
            seen,
            run_id=started.run.id,
            conversation_id=started.run.conversation_id,
            follows=started.run.message_id,
            ended=True,
        )
