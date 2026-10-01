# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""``PostgresConversationStore`` against a real PostgreSQL.

The whole of ``tests/contracts/conversation_store.py`` and
``conversation_runs.py`` -- one suite over one store, the suite the in-memory
fake passes -- run against the database, which is the point of having written
it once. What the store must do is said in the port and checked in the
contract; a promise tested only in this file would be a promise the fake is
free to break.

What is here is what only a database can be asked:

- that the pool really hands out several connections, so the contract's
  concurrency tests are not passing one statement at a time;
- that a **document** goes to a ``jsonb`` column and comes back equal -- the
  awkward shapes included: unicode that is more than one code point per
  character, a megabyte of text, and a nested ``extras`` object this build
  writes none of and must still keep;
- that a time survives the round trip as the instant it was, whatever zone the
  server and the session are in;
- that the constraints the store translates by **name** are really in the
  schema, the partial unique index among them: the store's refusals are
  nothing without them;
- that deleting a user takes their conversations, which is a chain of foreign
  keys and not a line of Python;
- that the lock order holds: six methods run at once on one conversation,
  many times over, and no deadlock escapes;
- and that the whole of ``application.Turns`` runs a turn against **this**
  store, with the stored stream and the stored tree read back afterwards.

A conversation's owner is a real user (``conversations.owner_id`` references
``users``), so ``new_store`` puts the contract's two people in the ``users``
table before it hands the store over. That is the schema speaking, not a
fixture: the fake has no users at all, and the database will not hold a
conversation belonging to nobody.

Skipped, with the reason, when ``ROBINAUTS_TEST_DATABASE_URL`` is not set.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta, timezone

import asyncpg
import pytest

from aio import asyncio_test
from contracts.conversation_runs import (
    ConversationRunsContract,
    announced,
    completed,
    delta,
    over,
    started,
)
from contracts.conversation_store import SOMEBODY_ELSE
from conversations import (
    AGENT,
    CONVERSATION,
    OWNER,
    RUN,
    agent_definition,
    answer,
    at,
    conversation,
    offered,
    provenance,
    question,
    run,
)
from fakes import CountingIdSource, FakeClock, ScriptedAgent, says
from postgres import DATABASE_URL, TemporarySchema, requires_postgres, temporary_schema
from robinauts.legacy.adapters import AsyncioRunExecutor, MemoryRunSignals
from robinauts.legacy.application import Turns
from robinauts.legacy.core import (
    check_event_order,
    message_from_stored,
    message_to_data,
    run_event_from_stored,
    run_event_to_data,
    transition,
    tree_of_stored,
)
from robinauts.legacy.datastore import (
    PostgresConversationStore,
    PostgresCredentialStore,
    create_schema,
)
from robinauts.legacy.datastore.conversations import (
    CONVERSATION_REFUSALS,
    RETRIED,
    RUN_ENDED_KIND,
)
from robinauts.legacy.domain import (
    FIRST_POSITION,
    Engine,
    InvalidValueError,
    Message,
    MessagePart,
    RobinautsError,
    Role,
    Run,
    RunEnded,
    RunState,
    TextPart,
)
from robinauts.legacy.ports import ConversationStore, Document

pytestmark = requires_postgres

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)

KATHMANDU = timezone(timedelta(hours=5, minutes=45))
"""An offset with a quarter hour in it: a zone that catches a wrong guess."""

CHATHAM = "Pacific/Chatham"
"""A session zone at +12:45 or +13:45, and not the one the server is on."""

PEOPLE = (OWNER, SOMEBODY_ELSE)
"""The two people the contract's conversations belong to.

A conversation's owner is a row of ``users`` -- the schema says so, and it is
how deleting an account takes the account's conversations with it -- so they
are there before the suite begins. Nothing else is: every call of
``new_store`` gives a schema of its own with no conversation, message, run or
event in it.
"""

TABLES = ("conversations", "messages", "runs", "run_events")
"""What this store owns, and what ``dump`` writes out, in one fixed order."""

_ORDER = {
    "conversations": "id",
    "messages": "id",
    "runs": "id",
    "run_events": "run_id, seq",
}
"""What each table is dumped in order of, so that two dumps of one database
are two equal strings and the contract may compare them."""


async def seeded(pool: asyncpg.Pool) -> None:
    """Put the contract's people in ``users``, which conversations point at."""
    for number, person in enumerate(PEOPLE):
        await pool.execute(
            "INSERT INTO users (id, provider, subject, created_at) VALUES ($1, $2, $3, $4)",
            person,
            "google",
            str(number),
            NOW,
        )


async def dumped(schema: TemporarySchema) -> str:
    """Every column of every row of this store's four tables, in a fixed order.

    Ordered on purpose: the contract compares a dump taken before a refused
    call with one taken after, and two reads of one table are only equal
    strings if something says what the order is.
    """
    lines = []
    async with schema.pool.acquire() as connection:
        for table in TABLES:
            rows = await connection.fetch(f"SELECT * FROM {table} ORDER BY {_ORDER[table]}")
            for row in rows:
                fields = ", ".join(f"{column}={value!r}" for column, value in row.items())
                lines.append(f"{table}: {fields}")
    return "\n".join(lines)


class TestPostgresConversationStore(ConversationRunsContract):
    """The whole store contract, on PostgreSQL, in a schema of this test's own."""

    def setup_method(self) -> None:
        self.schema = TemporarySchema()

    async def new_store(self) -> ConversationStore:
        pool = await self.schema.open()
        try:
            await create_schema(pool)
            await seeded(pool)
        except BaseException:
            # Between the pool and the schema there is a moment where a
            # failure would leave both behind: a schema on everybody's
            # database and eight connections on the server, for as long as
            # the process lives.
            await self.schema.close()
            raise
        return PostgresConversationStore(pool)

    async def close_store(self, store: ConversationStore | None) -> None:
        await self.schema.close()

    async def dump(self, store: ConversationStore) -> str:
        return await dumped(self.schema)

    @asyncio_test
    async def test_the_concurrency_tests_above_ran_against_a_real_pool(self) -> None:
        # A store on one connection would pass every `asyncio.gather` test in
        # the contract by doing the calls one after another. This is what says
        # they did not: the pool takes more than one connection, and four
        # statements started together really are on more than one backend.
        async with self.opened() as store:
            assert isinstance(store, PostgresConversationStore)
            assert self.schema.pool.get_max_size() > 1

            backends = await asyncio.gather(
                *(self.schema.pool.fetchval("SELECT pg_backend_pid()") for _ in range(4))
            )

            assert len(set(backends)) > 1


# What only a database can be asked.


@asyncio_test
async def test_a_document_comes_back_exactly_as_it_was_given() -> None:
    # `jsonb` is a value, not the text it was written as: it drops whitespace
    # and does not keep the order the keys were written in. None of that may
    # change the document, because everything above this store reads the
    # document back and expects what it wrote -- unicode that is several code
    # points to the character, an answer at the bound of what a part holds,
    # and the `extras` object this build writes none of and must still keep.
    unicode = "\U0001f9d1‍\U0001f680 héllo — Ω ✅ 中文 مرحبا é"
    long = "x" * 1_000_000
    extras = {"vendor": {"nested": [1, 2.5, True, None, {"deep": "é"}], "z": "1"}}
    async with temporary_schema() as schema:
        await seeded(schema.pool)
        store = PostgresConversationStore(schema.pool)
        await store.add_conversation(conversation())
        asked = question(unicode, seconds=0)
        replied = answer(asked, long, seconds=1, parts=_parts(long))
        # Keys in an order no caller would choose, and one the server will not
        # keep: what comes back is the same mapping all the same.
        document = dict(reversed(list(message_to_data(asked).items())))
        document["extras"] = extras

        await store.append_message(asked, document, now=at(1))
        await store.append_message(replied, message_to_data(replied), now=at(2))

        first, second = await store.messages_of(CONVERSATION)
        assert first == document
        assert first["extras"] == extras
        assert message_from_stored(first).text == unicode
        assert message_from_stored(second).text == long
        # And what came back is nobody else's mapping: editing it changes
        # nothing, here or anywhere.
        first["parts"] = "mutated"
        again, _ = await store.messages_of(CONVERSATION)
        assert again == document


@asyncio_test
async def test_an_event_document_comes_back_exactly_as_it_was_given() -> None:
    async with temporary_schema() as schema:
        await seeded(schema.pool)
        store = PostgresConversationStore(schema.pool)
        asked = question(seconds=0)
        await store.start_run(
            conversation=conversation(),
            message=(asked, message_to_data(asked)),
            run=run(message_id=asked.id),
            now=at(1),
        )
        event = started(RUN)
        document = dict(run_event_to_data(event))
        document["extras"] = {"kept": ["whole", {"and": "unread"}]}

        await store.append_event(event, document)

        (read,) = await store.events_of(RUN)
        assert read == document
        assert run_event_from_stored(read).seq == FIRST_POSITION


@asyncio_test
async def test_a_time_comes_back_as_the_instant_it_went_in() -> None:
    # The server this runs against is deliberately not on UTC, and the session
    # below is on a zone odder still. Neither may change what a stored time
    # means: `timestamptz` keeps an instant, and a conversation ordered by a
    # wall clock would sit in the panel at whatever place the reader's offset
    # put it.
    async with temporary_schema(server_settings={"timezone": CHATHAM}) as schema:
        await seeded(schema.pool)
        store = PostgresConversationStore(schema.pool)
        assert await schema.pool.fetchval("SELECT current_setting('TimeZone')") == CHATHAM
        # One instant, written with a quarter-hour offset.
        created_at = datetime(2026, 9, 21, 17, 45, tzinfo=KATHMANDU)
        asked = question(seconds=0, created_at=created_at)

        await store.add_conversation(conversation(created_at=created_at, updated_at=created_at))
        await store.append_message(asked, message_to_data(asked), now=created_at)
        await store.start_run(
            conversation=None,
            message=None,
            run=run(message_id=asked.id, created_at=created_at, started_at=created_at),
            now=created_at,
        )

        found = await store.conversation_by_id(CONVERSATION)
        going = await store.run_by_id(RUN)
        assert found is not None
        assert going is not None
        for when in (found.created_at, found.updated_at, going.created_at, going.started_at):
            assert when is not None
            assert when.tzinfo is not None
            assert when == created_at
            assert when == datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


@asyncio_test
async def test_a_naive_time_is_refused_rather_than_read_as_utc() -> None:
    # asyncpg would take a naive datetime for a `timestamptz` and call it UTC.
    # A deployment five and three quarter hours from UTC would then date every
    # conversation by that much, and nothing would have said so.
    naive = datetime(2026, 9, 21, 12, 0)
    async with temporary_schema() as schema:
        await seeded(schema.pool)
        store = PostgresConversationStore(schema.pool)
        await store.add_conversation(conversation())
        asked = question(seconds=0)

        with pytest.raises(ValueError, match="aware datetime"):
            await store.rename_conversation(CONVERSATION, "Mine", now=naive)
        with pytest.raises(ValueError, match="aware datetime"):
            await store.set_model(CONVERSATION, "gpt-5-5", now=naive)
        with pytest.raises(ValueError, match="aware datetime"):
            await store.touch_conversation(CONVERSATION, now=naive)
        with pytest.raises(ValueError, match="aware datetime"):
            await store.append_message(asked, message_to_data(asked), now=naive)
        with pytest.raises(ValueError, match="aware datetime"):
            await store.delete_conversation(CONVERSATION, now=naive)

        assert await store.messages_of(CONVERSATION) == ()


@pytest.mark.parametrize("named", sorted(CONVERSATION_REFUSALS))
@asyncio_test
async def test_every_constraint_the_store_answers_for_is_really_there(named: str) -> None:
    # The store turns a violation into an answer for its caller and tells one
    # violation from another by the constraint's **name**. A name that is not
    # in the schema is a refusal that can never happen: the rule would be
    # kept only by the look before the write, and the race the constraint
    # exists to catch would go through.
    async with temporary_schema() as schema:
        constraints = await schema.pool.fetch(
            "SELECT conname AS name FROM pg_constraint"
            " WHERE connamespace = current_schema()::regnamespace"
        )
        indexes = await schema.pool.fetch(
            "SELECT indexname AS name FROM pg_indexes WHERE schemaname = current_schema()"
        )

        there = {row["name"] for row in constraints} | {row["name"] for row in indexes}
        assert named in there


@asyncio_test
async def test_at_most_one_active_run_is_a_partial_unique_index() -> None:
    # Not a count in a transaction that has not committed: two `start_run`s
    # arriving together would both count none. The store looks first, under
    # the conversation's row lock, and this is what is underneath -- so it is
    # checked for being an index, for being unique, and for being over the
    # active states alone (a conversation has any number of ended runs).
    async with temporary_schema() as schema:
        definition = await schema.pool.fetchval(
            "SELECT indexdef FROM pg_indexes"
            " WHERE schemaname = current_schema() AND indexname = $1",
            "runs_one_active_per_conversation",
        )

        assert definition is not None
        assert "CREATE UNIQUE INDEX" in definition
        assert "(conversation_id)" in definition
        assert "'running'" in definition
        assert "'waiting'" in definition
        assert "'finished'" not in definition


@asyncio_test
async def test_a_second_active_run_is_refused_by_the_index_as_well() -> None:
    # The look under the lock is what a caller normally meets; this is the
    # backstop, shown to bite by writing a row that goes around the store.
    async with temporary_schema() as schema:
        await seeded(schema.pool)
        store = PostgresConversationStore(schema.pool)
        asked = question(seconds=0)
        await store.start_run(
            conversation=conversation(),
            message=(asked, message_to_data(asked)),
            run=run(message_id=asked.id),
            now=at(1),
        )

        with pytest.raises(asyncpg.exceptions.UniqueViolationError) as raw:
            await schema.pool.execute(
                "INSERT INTO runs (id, conversation_id, message_id, agent, engine, model,"
                " state, created_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                uuid.uuid4(),
                CONVERSATION,
                asked.id,
                AGENT,
                Engine.PYDANTIC_AI.value,
                "sonnet",
                RunState.RUNNING.value,
                NOW,
            )

        assert raw.value.constraint_name == "runs_one_active_per_conversation"


@asyncio_test
async def test_a_constraint_this_code_never_heard_of_is_not_an_answer() -> None:
    # The refusal table is read by name, so a constraint nobody planned to
    # violate is a bug and arrives as one rather than as a refusal about
    # something else entirely.
    async with temporary_schema() as schema:
        await seeded(schema.pool)
        await schema.pool.execute("CREATE UNIQUE INDEX one_each ON messages (conversation_id)")
        store = PostgresConversationStore(schema.pool)
        await store.add_conversation(conversation())
        asked = question(seconds=0)
        await store.append_message(asked, message_to_data(asked), now=at(1))
        again = answer(asked, seconds=2)

        with pytest.raises(asyncpg.exceptions.UniqueViolationError) as raw:
            await store.append_message(again, message_to_data(again), now=at(2))

        assert raw.value.constraint_name == "one_each"


@asyncio_test
async def test_deleting_a_user_takes_their_conversations_with_them() -> None:
    # The whole of it is foreign keys: the conversation cascades from the
    # user, the messages and runs from the conversation, the events from the
    # run. Nothing here is a line of Python that somebody could forget to
    # write, and the account that is the only thing entitled to read that
    # history is what is being removed.
    async with temporary_schema() as schema:
        credentials = PostgresCredentialStore(schema.pool)
        store = PostgresConversationStore(schema.pool)
        person = await credentials.user_at_sign_in("google", "1", name="Ada", email=None, now=NOW)
        asked = question(seconds=0)
        await store.start_run(
            conversation=conversation(owner_id=person.id),
            message=(asked, message_to_data(asked)),
            run=run(message_id=asked.id),
            now=at(1),
        )
        await store.append_event(started(RUN), run_event_to_data(started(RUN)))

        await schema.pool.execute("DELETE FROM users WHERE id = $1", person.id)

        assert await store.conversation_by_id(CONVERSATION) is None
        assert await store.messages_of(CONVERSATION) == ()
        assert await store.run_by_id(RUN) is None
        assert await store.events_of(RUN) == ()
        assert await dumped(schema) == ""


@asyncio_test
async def test_a_runs_stream_ends_once_whatever_anybody_writes() -> None:
    # The store refuses everything into a run that has ended, decided from the
    # run's own row. This says the same thing about the stream itself, so that
    # a second `RunEnded` cannot exist even for a writer that went around the
    # store -- and it is where the name in `RUN_ENDED_KIND` and the literal in
    # the schema are shown to be one thing.
    async with temporary_schema() as schema:
        await seeded(schema.pool)
        store = PostgresConversationStore(schema.pool)
        asked = question(seconds=0)
        await store.start_run(
            conversation=conversation(),
            message=(asked, message_to_data(asked)),
            run=run(message_id=asked.id),
            now=at(1),
        )
        await store.append_event(started(RUN), run_event_to_data(started(RUN)))
        kept = await store.run_by_id(RUN)
        assert kept is not None
        ending = over(RUN, FIRST_POSITION + 1, RunState.FINISHED)
        await store.end_run(
            transition(kept, RunState.FINISHED, now=at(8)), ending, run_event_to_data(ending)
        )

        kinds = await schema.pool.fetch(
            "SELECT kind FROM run_events WHERE run_id = $1 ORDER BY seq", RUN
        )
        assert [row["kind"] for row in kinds] == ["RunStarted", RUN_ENDED_KIND]

        with pytest.raises(asyncpg.exceptions.UniqueViolationError) as raw:
            await schema.pool.execute(
                "INSERT INTO run_events (run_id, seq, kind, document)"
                " VALUES ($1, $2, $3, $4::jsonb)",
                RUN,
                FIRST_POSITION + 2,
                RUN_ENDED_KIND,
                json.dumps(run_event_to_data(over(RUN, FIRST_POSITION + 2, RunState.CANCELLED))),
            )

        assert raw.value.constraint_name == "run_events_one_end_per_run"


@asyncio_test
async def test_six_methods_at_once_on_one_conversation_never_deadlock(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Every writing method takes the conversation's row first and the run's
    # second, which is what stops two of them closing a cycle. Nothing proves
    # an order is consistent except running them together, so this runs the
    # six that meet on one conversation -- completing a message, ending the
    # run, deleting, starting another run, renaming, changing the model --
    # many times over, and then runs the ones that meet a conversation whose
    # run has **ended** -- deleting, appending, renaming, changing the model,
    # starting a run -- where the delete is not refused and really cascades
    # through the messages, the runs and the events while the others are
    # writing.
    #
    # Two things are asked of every round: that nothing came out of the store
    # but its own refusals, and that what is left can be read back. And one
    # thing of the whole: that no transaction was retried, because the store
    # retries a deadlock and a retry is exactly how a lock order somebody got
    # wrong stays invisible.
    rounds = 40
    caplog.set_level(logging.WARNING, logger="robinauts.legacy.datastore.conversations")
    async with temporary_schema() as schema:
        await seeded(schema.pool)
        store = PostgresConversationStore(schema.pool)
        for _ in range(rounds):
            await _while_answering(store)
            await _after_the_answer(store)

        assert [record.getMessage() for record in caplog.records] == []


async def _while_answering(store: ConversationStore) -> None:
    """One round over a conversation with a run in flight."""
    here, asked, first, kept = await _begun_here(store)
    replied = answer(asked, conversation_id=here, seconds=3, provenance=provenance(run_id=first))
    seq = await _announced(store, first, replied)
    done = completed(first, seq + 1, replied)
    stopped = over(first, seq + 1, RunState.CANCELLED)

    outcomes = await asyncio.gather(
        store.complete_message(
            replied, message_to_data(replied), done, run_event_to_data(done), now=at(3)
        ),
        store.end_run(
            transition(kept, RunState.CANCELLED, now=at(4)), stopped, run_event_to_data(stopped)
        ),
        store.delete_conversation(here, now=at(9)),
        store.start_run(
            conversation=None,
            message=None,
            run=run(id=uuid.uuid4(), conversation_id=here, message_id=asked.id),
            now=at(5),
        ),
        store.rename_conversation(here, "Renamed", now=at(6)),
        store.set_model(here, "gpt-5-5", now=at(7)),
        return_exceptions=True,
    )

    _only_refusals(outcomes)
    await _consistent(store, here, asked.id)


async def _after_the_answer(store: ConversationStore) -> None:
    """One round over a conversation whose run has ended.

    The delete is not refused here, so it takes the conversation's row and
    then cascades -- the messages, the run, its events -- while an append is
    inserting a message under one of those very rows and a turn is starting
    another run. That is the meeting the first round never reaches, because
    a conversation that is answering is not deleted at all.
    """
    here, asked, first, kept = await _begun_here(store)
    replied = answer(asked, conversation_id=here, seconds=3, provenance=provenance(run_id=first))
    seq = await _announced(store, first, replied)
    done = completed(first, seq + 1, replied)
    await store.complete_message(
        replied, message_to_data(replied), done, run_event_to_data(done), now=at(3)
    )
    stopped = over(first, seq + 2, RunState.FINISHED)
    await store.end_run(
        transition(kept, RunState.FINISHED, now=at(4)), stopped, run_event_to_data(stopped)
    )
    again = question("And then?", parent=replied, conversation_id=here, seconds=5)

    outcomes = await asyncio.gather(
        store.delete_conversation(here, now=at(9)),
        store.append_message(again, message_to_data(again), now=at(5)),
        store.rename_conversation(here, "Renamed", now=at(6)),
        store.set_model(here, "gpt-5-5", now=at(6)),
        store.start_run(
            conversation=None,
            message=None,
            run=run(id=uuid.uuid4(), conversation_id=here, message_id=asked.id),
            now=at(7),
        ),
        return_exceptions=True,
    )

    _only_refusals(outcomes)
    await _consistent(store, here, asked.id)


async def _begun_here(store: ConversationStore) -> tuple[uuid.UUID, Message, uuid.UUID, Run]:
    """A new conversation, its question, and the run answering it, begun."""
    here = uuid.uuid4()
    asked = question(conversation_id=here, seconds=0)
    first = uuid.uuid4()
    await store.start_run(
        conversation=conversation(id=here),
        message=(asked, message_to_data(asked)),
        run=run(id=first, conversation_id=here, message_id=asked.id),
        now=at(1),
    )
    begun = started(first, conversation_id=here)
    await store.append_event(begun, run_event_to_data(begun))
    kept = await store.run_by_id(first)
    assert kept is not None
    return here, asked, first, kept


def _only_refusals(outcomes: list[object]) -> None:
    """Nothing came out of the store but the answers the port names.

    A deadlock, a serialization failure or any other driver error escaping
    here is the lock order being wrong.
    """
    for outcome in outcomes:
        assert not isinstance(outcome, asyncpg.PostgresError), outcome
        assert not isinstance(outcome, BaseException) or isinstance(
            outcome, RobinautsError
        ), outcome


async def _announced(store: ConversationStore, run_id: uuid.UUID, message: Message) -> int:
    """Announce that a message is being produced; the position that took."""
    seq = await store.last_position(run_id) + 1
    event = announced(run_id, seq, message)
    await store.append_event(event, run_event_to_data(event))
    return seq


async def _consistent(
    store: ConversationStore, conversation_id: uuid.UUID, follows: uuid.UUID
) -> None:
    """Whatever happened in that round, what is stored still makes sense.

    Either the conversation went, with everything under it, or it is there
    with at most one run going and every run's stream readable by
    ``core.check_event_order`` -- which is the whole point of the store's
    rules about positions and ended runs.
    """
    found = await store.conversation_by_id(conversation_id)
    runs = await store.runs_of(conversation_id)
    if found is None:
        assert runs == ()
        assert await store.messages_of(conversation_id) == ()
        return
    assert sum(1 for kept in runs if kept.is_active) <= 1
    for kept in runs:
        documents = await store.events_of(kept.id)
        if documents:
            _stream_reads_back(documents, conversation_id, kept.id, follows)


def _stream_reads_back(
    documents: tuple[Document, ...],
    conversation_id: uuid.UUID,
    run_id: uuid.UUID,
    follows: uuid.UUID,
) -> None:
    """What is in the events table is a stream ``core`` can read back.

    The check every one of this store's rules about positions and ended runs
    exists to make possible, asked of the rows as they really are.
    """
    events = [run_event_from_stored(document) for document in documents]
    check_event_order(
        events,
        run_id=run_id,
        conversation_id=conversation_id,
        follows=follows,
        ended=isinstance(events[-1].event, RunEnded),
    )


@asyncio_test
async def test_a_whole_turn_runs_against_this_store() -> None:
    # The run lifecycle of step 10, over the real store rather than the fake:
    # one streamed turn, from a request to a stored answer. What is asserted
    # is what a browser would go on to read -- the tree, and the stream a
    # watcher re-attaches to -- read back out of PostgreSQL.
    async with temporary_schema() as schema:
        credentials = PostgresCredentialStore(schema.pool)
        store = PostgresConversationStore(schema.pool)
        author = await credentials.user_at_sign_in("google", "1", name="Ada", email=None, now=NOW)
        definition = agent_definition()
        turns = Turns(
            store=store,
            clock=FakeClock(now=at(100)),
            ids=CountingIdSource(),
            agents={definition.id: definition},
            models=offered(),
            engines={definition.engine: ScriptedAgent(*says("Someone who plays fair."))},
            executor=AsyncioRunExecutor(),
            signals=MemoryRunSignals(),
        )

        begun = await turns.start(author, agent_id=AGENT, text="What is a robinaut?")
        await turns.execute(begun.run)

        ended = await store.run_by_id(begun.run.id)
        assert ended is not None
        assert ended.state is RunState.FINISHED
        documents = await store.messages_of(begun.conversation.id)
        tree = tree_of_stored(
            [message_from_stored(document) for document in documents],
            conversation_id=begun.conversation.id,
        )
        root, replied = tree.visible_path()
        assert root.role is Role.USER
        assert root.text == "What is a robinaut?"
        assert replied.role is Role.ASSISTANT
        assert replied.text == "Someone who plays fair."
        assert replied.provenance is not None
        assert replied.provenance.run_id == ended.id
        events = [run_event_from_stored(document) for document in await store.events_of(ended.id)]
        check_event_order(
            events,
            run_id=ended.id,
            conversation_id=ended.conversation_id,
            follows=ended.message_id,
            ended=True,
        )
        assert await store.active_run_of(begun.conversation.id) is None


def _parts(text: str) -> tuple[MessagePart, ...]:
    """One part holding all of that text: an answer at the bound of one."""
    return (TextPart(text),)


@asyncio_test
async def test_a_document_json_cannot_be_written_from_is_refused() -> None:
    # The store promises a document comes back equal to what it was given.
    # Three ways a caller can hand it one that cannot: a key that is not text
    # (`json` renames `1` to `"1"` without a word, and a mapping holding both
    # comes back with one of them gone -- silent, and it loses data), a value
    # JSON has no shape for, and NaN or an infinity, which `json` writes as
    # bare words PostgreSQL refuses. All three are the caller's, so all three
    # are refusals of the port rather than a `TypeError` or a driver error.
    async with temporary_schema() as schema:
        await seeded(schema.pool)
        store = PostgresConversationStore(schema.pool)
        await store.add_conversation(conversation())
        asked = question(seconds=0)
        whole = message_to_data(asked)
        # Deeper than the writer will walk: a RecursionError where a refusal
        # belongs, which is why the walk that looks at the keys is iterative
        # and the writer's own is caught.
        deep: object = "the bottom"
        for _ in range(20_000):
            deep = {"down": deep}

        for broken in (
            {**whole, 1: "a key that is not text"},
            {**whole, "extras": {2: "a nested key that is not text"}},
            {**whole, "extras": [{"deeper": [{None: "nor is this"}]}]},
            {**whole, "extras": {"a set": {1, 2}}},
            {**whole, "extras": {"not a number": float("nan")}},
            {**whole, "extras": {"nor this": float("inf")}},
            {**whole, "extras": deep},
        ):
            with pytest.raises(InvalidValueError):
                await store.append_message(asked, broken, now=at(1))

        # Nothing of any of them reached the server.
        assert await store.messages_of(CONVERSATION) == ()
        # And the one that merely *looks* odd -- a key that is text and a
        # value JSON has a shape for -- goes in and comes back.
        fine = {**whole, "extras": {"1": "one", "1.0": "another"}}
        await store.append_message(asked, fine, now=at(1))
        assert list(await store.messages_of(CONVERSATION)) == [fine]


@asyncio_test
async def test_appending_an_event_costs_the_same_however_long_the_run_is() -> None:
    # The check before every write must not read the run's events, or a run
    # costs the square of its own length: at twenty thousand deltas that is
    # the difference between a minute and no time at all. `_LAST_POSITION` is
    # one backward walk of `run_events_pkey`, so this holds however far the
    # stream has got. The bound is generous on purpose -- this is about the
    # shape of the cost, not about how quick a laptop is -- and it is checked
    # on the LAST thousand rather than on the average, so a cost that grows
    # with the stream fails it even inside a generous total.
    events = 5_000
    async with temporary_schema() as schema:
        await seeded(schema.pool)
        store = PostgresConversationStore(schema.pool)
        asked = question(seconds=0)
        await store.start_run(
            conversation=conversation(),
            message=(asked, message_to_data(asked)),
            run=run(message_id=asked.id),
            now=at(1),
        )
        begun = started(RUN)
        await store.append_event(begun, run_event_to_data(begun))

        first = time.monotonic()
        for seq in range(FIRST_POSITION + 1, FIRST_POSITION + events - 1_000):
            event = delta(RUN, seq)
            await store.append_event(event, run_event_to_data(event))
        middle = time.monotonic()
        for seq in range(FIRST_POSITION + events - 1_000, FIRST_POSITION + events):
            event = delta(RUN, seq)
            await store.append_event(event, run_event_to_data(event))
        last = time.monotonic()

        assert await store.last_position(RUN) == FIRST_POSITION + events - 1
        early = (middle - first) / (events - 1_000 - 1)
        late = (last - middle) / 1_000
        assert late < 0.01, f"{late * 1000:.2f} ms an event at {events} events"
        # And the last thousand cost about what the first few thousand did.
        # A read of the whole stream per write would make this ratio the
        # ratio of the lengths, which at these numbers is four or five.
        assert late < early * 2.5, f"{early * 1000:.3f} ms early, {late * 1000:.3f} ms late"


@asyncio_test
async def test_a_real_deadlock_is_retried_once_and_then_succeeds(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The retry loop, against a deadlock PostgreSQL really reports. An
    # outsider connection takes the two rows in the order this store never
    # does -- the run's, then the conversation's -- so that `complete_message`
    # holding the conversation and wanting the run is one half of a cycle.
    # PostgreSQL picks a victim; when it picks ours, the store must try the
    # whole transaction again, say so once, and get it right.
    caplog.set_level(logging.WARNING, logger="robinauts.legacy.datastore.conversations")
    async with temporary_schema() as schema:
        await seeded(schema.pool)
        store = PostgresConversationStore(schema.pool)
        asked = question(seconds=0)
        await store.start_run(
            conversation=conversation(),
            message=(asked, message_to_data(asked)),
            run=run(message_id=asked.id),
            now=at(1),
        )
        begun = started(RUN)
        await store.append_event(begun, run_event_to_data(begun))
        replied = answer(asked, seconds=3)
        announcing = announced(RUN, FIRST_POSITION + 1, replied)
        await store.append_event(announcing, run_event_to_data(announcing))
        done = completed(RUN, FIRST_POSITION + 2, replied)

        assert DATABASE_URL is not None
        outsider = await asyncpg.connect(DATABASE_URL, server_settings={"search_path": schema.name})
        try:
            held_by = await outsider.fetchval("SELECT pg_backend_pid()")
            holding = outsider.transaction()
            await holding.start()
            # The wrong order, on purpose: the run first.
            await outsider.fetchrow("SELECT id FROM runs WHERE id = $1 FOR UPDATE", RUN)

            completing = asyncio.create_task(
                store.complete_message(
                    replied, message_to_data(replied), done, run_event_to_data(done), now=at(3)
                )
            )
            # Wait until the store is holding the conversation and waiting on
            # the run, which is the only moment the cycle can be closed.
            waiting = await _blocked_by(schema.pool, held_by)
            if not waiting:  # pragma: no cover - a server too slow to provoke
                completing.cancel()
                await asyncio.gather(completing, return_exceptions=True)
                pytest.skip("the store never got as far as waiting for the run's row")

            # And now the other half: the conversation, which the store holds.
            # One of the two transactions is now a cycle, and PostgreSQL ends
            # whichever of them notices first -- which is the one that has
            # been waiting longest, the store's.
            closed = asyncio.create_task(
                outsider.fetchrow(
                    "SELECT id FROM conversations WHERE id = $1 FOR UPDATE", CONVERSATION
                )
            )
            (outside,) = await asyncio.wait_for(
                asyncio.gather(closed, return_exceptions=True), timeout=30
            )
        finally:
            # Whoever lost, the outsider's locks go now: the store's retry
            # wants the very row this transaction is holding, so a rollback
            # left to the end of the test would be a test that hangs.
            with contextlib.suppress(Exception):
                await holding.rollback()
            await outsider.close()

        (inside,) = await asyncio.wait_for(
            asyncio.gather(completing, return_exceptions=True), timeout=30
        )

        if isinstance(outside, asyncpg.DeadlockDetectedError):  # pragma: no cover
            # The server ended the outsider instead. Nothing was asked of the
            # retry, so there is nothing to say about it.
            pytest.skip("the outsider was the deadlock's victim, not the store")
        # The store was never the one that failed: it was told to try again,
        # said so once, and then wrote both halves.
        assert not isinstance(inside, BaseException), inside
        assert [record.getMessage() for record in caplog.records] == [
            RETRIED % "DeadlockDetectedError"
        ]
        assert len(await store.messages_of(CONVERSATION)) == 2
        assert await store.last_position(RUN) == FIRST_POSITION + 2
        _stream_reads_back(await store.events_of(RUN), CONVERSATION, RUN, asked.id)


async def _blocked_by(pool: asyncpg.Pool, holder: int, *, seconds: float = 10.0) -> bool:
    """Wait until some backend is waiting on a lock that backend holds.

    Asked of `pg_blocking_pids`, not guessed at with a sleep: the test is
    about one moment -- the store holding the conversation and waiting for
    the run -- and a moment a sleep happens to land on is a flake. Naming the
    holder is also what keeps another session's waiting, in a run beside this
    one, from being mistaken for ours.
    """
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        waiting = await pool.fetchval(
            "SELECT true FROM pg_stat_activity WHERE $1 = ANY(pg_blocking_pids(pid)) LIMIT 1",
            holder,
        )
        if waiting:
            return True
        await asyncio.sleep(0.02)
    return False
