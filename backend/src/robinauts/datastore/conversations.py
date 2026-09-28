# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The conversation store on PostgreSQL: conversations, messages, runs, events.

``robinauts.ports.ConversationStore`` over an ``asyncpg`` pool it is
**given**, as ``PostgresCredentialStore`` is: it opens nothing and closes
nothing, and the pool is made in the composition root and outlives every
store (``docs/layout.md``). It is held to the same contract suite as the
in-memory fake -- ``tests/contracts/conversation_store.py`` and
``conversation_runs.py``, which are one suite over one store -- and that is
where the promises below are written down and checked.

**One transaction per method.** The port's compound operations are
indivisible by definition: beginning a turn creates a conversation, appends
its question and creates the run; completing a message stores the message,
moves the conversation and appends the announcement; ending a run writes the
ended record and the event that says so; deleting a conversation refuses
while a run is going and otherwise takes everything under it. Each of those
is one ``BEGIN`` ... ``COMMIT``, and a refusal inside it leaves nothing
behind, because the rollback is what "a refused call writes nothing" is made
of. The methods that are one statement are one statement: a statement is a
transaction, and wrapping one in another would only be ceremony.

**Locks are always taken in one order: the conversation's row, then the
run's.** Two methods that took them the other way round would deadlock the
first time they met on one conversation, and a deadlock is a failure a caller
cannot do anything about. So:

- ``start_run``, ``append_message``, ``complete_message`` and
  ``delete_conversation`` take the conversation's row
  first, with ``SELECT ... FOR UPDATE`` (``start_run`` creates it instead,
  where it is creating one -- a row it has just inserted is one nobody else
  can see);
- ``complete_message`` then takes the run's row; ``append_event``,
  ``update_run`` and ``end_run`` take the run's row and nothing else, which
  is the tail of the same order and so cannot close a cycle;
- the reads take nothing.

Holding the conversation's row is also what makes "at most one active run"
and "the next position" decidable by *looking*: while it is held, nothing
else starts a run in that conversation, and while a run's row is held,
nothing else writes that run's events. The unique index on the active run and
the ``(run_id, seq)`` primary key are the **backstops** under those two
checks, not the mechanism -- and, being backstops, they are translated by
constraint name into exactly the refusals the port names.

Something can still deadlock -- two conversations written to in two orders by
one caller's two calls, a cascade meeting a lock -- so every transaction here
is tried again a bounded number of times (``MAX_ATTEMPTS``) when PostgreSQL
reports a deadlock or a serialization failure. Retrying is safe because the
transaction rolled back: there is nothing half-written to be repeated.

**The snapshot is one moment, at ``REPEATABLE READ``.** It reads four things
-- the conversation, its messages, the run in flight and that run's events --
and they must agree with each other, because the gap between two reads is
exactly where an answer is. Two ways would do: one enormous statement with
aggregated sub-selects, or a read-only transaction at ``REPEATABLE READ``,
where every statement sees the one snapshot the first of them took. This
takes the second: the four queries stay the four queries the other methods
use, which is worth more than saving three round trips, and a read-only
``REPEATABLE READ`` transaction cannot raise a serialization failure, so
there is nothing to retry and no isolation level left set on a pooled
connection (``BEGIN ISOLATION LEVEL ...`` is per transaction).

**A document crosses whole and is never read.** A message and a run event
arrive as the plain mapping the platform's format was written as
(``robinauts.core``), go into a ``jsonb`` column, and come back equal to what
they were given. ``json.dumps`` and ``json.loads`` do it here rather than a
codec installed on the pool: the store is *given* a pool it does not own, and
a store whose correctness depended on how somebody else built the pool would
be a store that breaks when a command opens its own. The columns beside the
document -- an id, a conversation, a parent, a role, a time, a position, a
kind -- are filled from the **record** passed with it, which is the only
thing this store looks at.

``jsonb`` is not a byte-for-byte store of the text it was given: it drops
insignificant whitespace, keeps one of a repeated key, and does not preserve
the order of keys in an object. None of that changes the *value* -- two
mappings that differ only in the order their keys were written are equal --
and the shapes the format writes have no repeated keys. What ``jsonb`` will
not take is a NUL inside a string, which the format refuses anyway
(``domain.checked_text``), and an unpaired surrogate, which it also refuses:
either would be an error here rather than a silent change.

**Times come in, and go out, as instants.** Every time is computed by the
application from its ``Clock`` and passed in; nothing here calls ``now()``.
A naive datetime is refused rather than read as UTC by asyncpg.

**Driver errors are left alone, except the named constraints that are
answers.** ``CONVERSATION_REFUSALS`` is the whole list, by constraint
**name**, because the exception class only says "some unique index in this
statement" and the day a second one is added to a table every violation of it
would be reported as the first. The names are written into ``schema.sql`` for
that reason, and a test keeps the two in step. A constraint this code has
never heard of is one nobody planned to violate, so it propagates as the
driver error it is.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import replace
from datetime import datetime

import asyncpg

from robinauts.domain import (
    ACTIVE_RUN_STATES,
    ENDED_RUN_STATES,
    MAX_TITLE_CHARS,
    Conversation,
    ConversationNotFoundError,
    Engine,
    IllegalTransitionError,
    InvalidCursorError,
    InvalidValueError,
    Message,
    MessageCompleted,
    MessageNotFoundError,
    PositionTakenError,
    Role,
    Run,
    RunAlreadyActiveError,
    RunEnded,
    RunEvent,
    RunNotFoundError,
    RunState,
    checked_config_id,
    checked_line,
    reading_stored,
)
from robinauts.ports import (
    MAX_PAGE,
    MAX_SWEPT,
    ConversationPage,
    ConversationStore,
    Document,
    Snapshot,
)

_log = logging.getLogger(__name__)

RETRIED = (
    "a conversation store transaction was retried after %s; if this is not rare, two"
    " statements are taking this deployment's row locks in two orders"
)
"""Logged every time a transaction is tried again, because a retry hides.

A store that silently swallowed a deadlock would turn a lock order somebody
got wrong into a deployment that is merely slow, and nothing would ever say
so. The lock order here is one order -- the conversation's row, then the
run's -- and this line is how an operator, and the integration test, find out
that it stopped being.
"""

MAX_ATTEMPTS = 5
"""How often one transaction is tried again before its failure is the answer.

Only for the two failures that mean "nothing happened, do it again":
PostgreSQL detected a deadlock, or a transaction could not be serialized.
Both leave the transaction rolled back, so a retry repeats no half of
anything. Bounded, because a store that retried for ever would turn a
mistake that deadlocks reliably into a request that never answers.
"""

RUN_ENDED_KIND = RunEnded.__name__
"""What ``run_events.kind`` holds for the event that ends a run.

The name of the record's class, which is also the literal in the partial
unique index ``run_events_one_end_per_run``; ``tests/unit/test_datastore_schema.py``
pins the two together. A store may not import ``robinauts.core``, so the
format's own spelling of a kind is not available here and is not wanted.

The column is written for that index and read by nothing: "a stream ends
once" is a property of the data, held by the index, rather than a question
asked before every write. What the writes ask is the run's **state**, which
is a row they hold a lock on and which says the same thing -- ``end_run``
writes the ended record and its ``RunEnded`` in one transaction, so a stream
that ends is a run that has ended.
"""

ACTIVE_STATES = tuple(sorted(state.value for state in ACTIVE_RUN_STATES))
"""The states a run is still going in, as the columns spell them.

The same set the partial indexes of ``schema.sql`` are built over; a state
added to one and not the other would make the index answer a different
question from the queries.
"""

CONVERSATION_REFUSALS: dict[str, tuple[type[Exception], str]] = {
    "conversations_pkey": (
        InvalidValueError,
        "conversation {conversation_id} is already stored",
    ),
    "conversations_owner_id_fkey": (
        InvalidValueError,
        "there is no user {owner_id} to own a conversation",
    ),
    "messages_pkey": (InvalidValueError, "message {message_id} is already stored"),
    "messages_conversation_id_fkey": (
        ConversationNotFoundError,
        "there is no conversation {conversation_id} to append to",
    ),
    "messages_parent_id_fkey": (
        MessageNotFoundError,
        "message {parent_id} is not in conversation {conversation_id}",
    ),
    "runs_pkey": (InvalidValueError, "run {run_id} is already stored"),
    "runs_conversation_id_fkey": (
        ConversationNotFoundError,
        "there is no conversation {conversation_id}",
    ),
    "runs_message_id_fkey": (
        MessageNotFoundError,
        "message {message_id} is not in conversation {conversation_id}",
    ),
    "runs_one_active_per_conversation": (
        RunAlreadyActiveError,
        "conversation {conversation_id} is already answering; cancel that run or wait for it",
    ),
    "run_events_pkey": (
        PositionTakenError,
        "position {seq} of run {run_id} is already stored",
    ),
    "run_events_run_id_fkey": (RunNotFoundError, "there is no run {run_id} to append to"),
}
"""The constraints that are an answer for a caller, by name, with their error.

Every one of them is a rule the port states, met here as a constraint because
the database is the only thing that can decide it without a window: an id
already taken, a row that is not there to hang something on, a second active
run, a position stored twice. Each is also checked by looking, under the lock
that makes looking safe; these are what is underneath, and what catches the
case the look cannot (two callers, two transactions, one instant).

Anything else -- a CHECK on a role, a state or an engine, the unique index
that stops a run ending twice, a constraint added later -- is not an answer
to give a caller. The records validate themselves before any of them can be
reached, so a violation means something is wrong here rather than with the
call, and it propagates.
"""

_CONVERSATION_COLUMNS = "id, owner_id, agent, model, title, created_at, updated_at"
_RUN_COLUMNS = (
    "id, conversation_id, message_id, agent, engine, model, state,"
    " created_at, started_at, finished_at, error"
)

_INSERT_CONVERSATION = f"""
INSERT INTO conversations ({_CONVERSATION_COLUMNS})
VALUES ($1, $2, $3, $4, $5, $6, $7)
"""

_LOCK_CONVERSATION = "SELECT agent FROM conversations WHERE id = $1 FOR UPDATE"
"""The conversation's row, held until this transaction ends.

The first lock every writing method takes, and what makes "is this
conversation already answering" decidable by looking: while it is held
nothing else writes this conversation or starts a run in it.
"""

_DATE_CONVERSATION = """
UPDATE conversations SET updated_at = $2 WHERE id = $1
"""
"""Date it: the one column an append moves.

Named columns, never the whole row read back and written again: a rename or a
model change landing between the read and the write would be lost, and the
contract has a test for each.
"""

_TOUCH_CONVERSATION = "UPDATE conversations SET updated_at = $2 WHERE id = $1"

_INSERT_MESSAGE = """
INSERT INTO messages (id, conversation_id, parent_id, role, created_at, document)
VALUES ($1, $2, $3, $4, $5, $6::jsonb)
"""

_INSERT_RUN = f"""
INSERT INTO runs ({_RUN_COLUMNS})
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
"""

_INSERT_EVENT = """
INSERT INTO run_events (run_id, seq, kind, document) VALUES ($1, $2, $3, $4::jsonb)
"""

_LOCK_RUN = f"SELECT {_RUN_COLUMNS} FROM runs WHERE id = $1 FOR UPDATE"

_ACTIVE_RUN = f"""
SELECT {_RUN_COLUMNS} FROM runs
WHERE conversation_id = $1 AND state = ANY($2::text[])
"""

_LAST_POSITION = "SELECT coalesce(max(seq), 0) FROM run_events WHERE run_id = $1"
"""How far a run's stream has got, asked once per write.

**Constant time, whatever the run has published.** ``max(seq)`` over a
``run_id`` is a backward walk of the first entry of ``run_events_pkey`` and
stops at the first row, so a run that has streamed a hundred thousand deltas
answers as quickly as one that has streamed none. It has to be: this runs
before **every** event, so anything that reads the run's events here is one
statement per event over a table that grows by one event -- a run that costs
the square of its own length, which at twenty thousand deltas is the
difference between a minute and no time at all.

That is also why "has this stream already ended" is not asked beside it. It
would be a second predicate over the same rows, and it is the same question
as the run's **state**, which the caller is already holding a lock on:
``end_run`` writes the ended record and its ``RunEnded`` in one transaction.
The backstop is not a query but the partial unique index
``run_events_one_end_per_run``, which makes a second end impossible rather
than merely refused.
"""


class PostgresConversationStore(ConversationStore):
    """Conversations, messages, runs and run events in PostgreSQL."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    # Conversations.

    async def add_conversation(self, conversation: Conversation) -> None:
        _typed(conversation, Conversation, "a conversation")
        try:
            await self._pool.execute(_INSERT_CONVERSATION, *_conversation_values(conversation))
        except asyncpg.exceptions.IntegrityConstraintViolationError as violation:
            _refused(
                violation,
                conversation_id=conversation.id,
                owner_id=conversation.owner_id,
            )

    async def conversation_by_id(self, conversation_id: uuid.UUID) -> Conversation | None:
        row = await self._pool.fetchrow(
            f"SELECT {_CONVERSATION_COLUMNS} FROM conversations WHERE id = $1", conversation_id
        )
        return None if row is None else _conversation(row)

    async def conversations_of(
        self, owner_id: uuid.UUID, *, limit: int, cursor: str | None = None
    ) -> ConversationPage:
        _page(limit)
        after = _cursor(cursor)
        # One more than was asked for, which is how "is there a next page"
        # is answered without a second query and without counting a table.
        if after is None:
            rows = await self._pool.fetch(
                f"""
                SELECT {_CONVERSATION_COLUMNS} FROM conversations
                WHERE owner_id = $1
                ORDER BY updated_at DESC, id DESC
                LIMIT $2::bigint
                """,
                owner_id,
                limit + 1,
            )
        else:
            # The keyset, both halves at once: a row comparison, which is what
            # the (owner_id, updated_at DESC, id DESC) index is walked by.
            rows = await self._pool.fetch(
                f"""
                SELECT {_CONVERSATION_COLUMNS} FROM conversations
                WHERE owner_id = $1 AND (updated_at, id) < ($2::timestamptz, $3::uuid)
                ORDER BY updated_at DESC, id DESC
                LIMIT $4::bigint
                """,
                owner_id,
                after[0],
                after[1],
                limit + 1,
            )
        page = tuple(_conversation(row) for row in rows[:limit])
        more = len(rows) > limit
        return ConversationPage(
            conversations=page, cursor=_spelt(page[-1]) if more and page else None
        )

    async def rename_conversation(
        self, conversation_id: uuid.UUID, title: str, *, now: datetime
    ) -> Conversation | None:
        # The same rule the record keeps, kept before the statement: a title
        # that is not one printable line is a value a caller gave, and
        # `Conversation` would refuse it. Without this the column takes
        # whatever it is handed -- a NUL, a newline, a megabyte -- and the
        # caller meets a driver error where the port promises a refusal, or,
        # worse, reads back a record the domain will not build.
        checked_line(title, "a conversation's title", MAX_TITLE_CHARS)
        _instant(now, "now")
        row = await self._pool.fetchrow(
            f"""
            UPDATE conversations SET title = $2, updated_at = $3 WHERE id = $1
            RETURNING {_CONVERSATION_COLUMNS}
            """,
            conversation_id,
            title,
            now,
        )
        return None if row is None else _conversation(row)

    async def set_model(
        self, conversation_id: uuid.UUID, model: str, *, now: datetime
    ) -> Conversation | None:
        # The record's rule, kept before the statement, for the reason the
        # rename gives: the column is plain text and would take anything.
        checked_config_id(model, "a model's id")
        _instant(now, "now")
        row = await self._pool.fetchrow(
            f"""
            UPDATE conversations SET model = $2, updated_at = $3 WHERE id = $1
            RETURNING {_CONVERSATION_COLUMNS}
            """,
            conversation_id,
            model,
            now,
        )
        return None if row is None else _conversation(row)

    async def touch_conversation(
        self, conversation_id: uuid.UUID, *, now: datetime
    ) -> Conversation | None:
        _instant(now, "now")
        row = await self._pool.fetchrow(
            f"""
            UPDATE conversations SET updated_at = $2 WHERE id = $1
            RETURNING {_CONVERSATION_COLUMNS}
            """,
            conversation_id,
            now,
        )
        return None if row is None else _conversation(row)

    async def delete_conversation(self, conversation_id: uuid.UUID, *, now: datetime) -> bool:
        _instant(now, "now")
        return await self._transacted(
            lambda connection: self._delete_conversation(connection, conversation_id)
        )

    async def _delete_conversation(
        self, connection: asyncpg.Connection, conversation_id: uuid.UUID
    ) -> bool:
        if await connection.fetchrow(_LOCK_CONVERSATION, conversation_id) is None:
            return False
        # Inside the same transaction as the delete, and with the row held, so
        # a run cannot begin in the window between the two.
        going = await connection.fetchrow(_ACTIVE_RUN, conversation_id, list(ACTIVE_STATES))
        if going is not None:
            raise RunAlreadyActiveError(
                f"run {going['id']} is {going['state']} in conversation {conversation_id};"
                " cancel it before deleting the conversation"
            )
        # The messages, the runs and the events go with it, by the foreign
        # keys of schema.sql: nothing here has to remember to delete them, and
        # nothing can be left orphaned by an order somebody got wrong.
        await connection.execute("DELETE FROM conversations WHERE id = $1", conversation_id)
        return True

    # Messages.

    async def append_message(self, message: Message, document: Document, *, now: datetime) -> None:
        _typed(message, Message, "a message")
        written = _document(document)
        _instant(now, "now")
        await self._transacted(
            lambda connection: self._append_message(connection, message, written, now)
        )

    async def _append_message(
        self, connection: asyncpg.Connection, message: Message, document: str, now: datetime
    ) -> None:
        if await connection.fetchrow(_LOCK_CONVERSATION, message.conversation_id) is None:
            raise ConversationNotFoundError(
                f"there is no conversation {message.conversation_id} to append to"
            )
        await self._store_message(connection, message, document)
        await connection.execute(_DATE_CONVERSATION, message.conversation_id, now)

    @staticmethod
    async def _store_message(
        connection: asyncpg.Connection, message: Message, document: str
    ) -> None:
        """The message row alone: the document, and the columns beside it."""
        try:
            await connection.execute(
                _INSERT_MESSAGE,
                message.id,
                message.conversation_id,
                message.parent_id,
                message.role.value,
                message.created_at,
                document,
            )
        except asyncpg.exceptions.IntegrityConstraintViolationError as violation:
            _refused(
                violation,
                message_id=message.id,
                conversation_id=message.conversation_id,
                parent_id=message.parent_id,
            )

    async def messages_of(self, conversation_id: uuid.UUID) -> tuple[Document, ...]:
        rows = await self._pool.fetch(
            "SELECT document FROM messages WHERE conversation_id = $1" " ORDER BY created_at, id",
            conversation_id,
        )
        return tuple(_read(row["document"]) for row in rows)

    async def conversation_snapshot(self, conversation_id: uuid.UUID) -> Snapshot:
        # Read-only and REPEATABLE READ: every statement below sees the one
        # snapshot the first of them took, so the four answers agree with each
        # other. See this module's docstring for why that rather than one
        # aggregated statement. A read-only transaction at this level raises
        # no serialization failure, so it needs no retry.
        async with self._pool.acquire() as connection:
            async with connection.transaction(isolation="repeatable_read", readonly=True):
                found = await connection.fetchrow(
                    f"SELECT {_CONVERSATION_COLUMNS} FROM conversations WHERE id = $1",
                    conversation_id,
                )
                if found is None:
                    return Snapshot()
                going = await connection.fetchrow(_ACTIVE_RUN, conversation_id, list(ACTIVE_STATES))
                messages = await connection.fetch(
                    "SELECT document FROM messages WHERE conversation_id = $1"
                    " ORDER BY created_at, id",
                    conversation_id,
                )
                events = (
                    ()
                    if going is None
                    else await connection.fetch(
                        "SELECT document FROM run_events WHERE run_id = $1 ORDER BY seq",
                        going["id"],
                    )
                )
        return Snapshot(
            conversation=_conversation(found),
            messages=tuple(_read(row["document"]) for row in messages),
            active_run=None if going is None else _run(going),
            events=tuple(_read(row["document"]) for row in events),
        )

    # Runs.

    async def start_run(
        self,
        *,
        conversation: Conversation | None,
        message: tuple[Message, Document] | None,
        run: Run,
        now: datetime,
    ) -> None:
        _typed(run, Run, "a run")
        if conversation is not None:
            _typed(conversation, Conversation, "a conversation")
        asked: Message | None = None
        written: str | None = None
        if message is not None:
            asked, document = message
            _typed(asked, Message, "a message")
            written = _document(document)
        _instant(now, "now")
        _check_start(conversation, asked, run)
        await self._transacted(
            lambda connection: self._start_run(connection, conversation, asked, written, run, now)
        )

    async def _start_run(
        self,
        connection: asyncpg.Connection,
        conversation: Conversation | None,
        asked: Message | None,
        document: str | None,
        run: Run,
        now: datetime,
    ) -> None:
        if conversation is not None:
            # Created rather than locked: a row this transaction has just
            # inserted is a row nobody else can see, which is the same
            # exclusion the lock gives on one that was already there. Two
            # turns creating one id meet on the primary key instead.
            try:
                await connection.execute(_INSERT_CONVERSATION, *_conversation_values(conversation))
            except asyncpg.exceptions.IntegrityConstraintViolationError as violation:
                _refused(
                    violation,
                    conversation_id=conversation.id,
                    owner_id=conversation.owner_id,
                )
        else:
            row = await connection.fetchrow(_LOCK_CONVERSATION, run.conversation_id)
            if row is None:
                raise ConversationNotFoundError(f"there is no conversation {run.conversation_id}")
            if row["agent"] != run.agent:
                raise InvalidValueError(
                    f"conversation {run.conversation_id} is bound to agent {row['agent']!r},"
                    f" and the run names {run.agent!r}"
                )
        going = await connection.fetchrow(_ACTIVE_RUN, run.conversation_id, list(ACTIVE_STATES))
        if going is not None:
            raise RunAlreadyActiveError(
                f"run {going['id']} is {going['state']} in conversation"
                f" {run.conversation_id}; cancel it or wait for it"
            )
        if asked is not None:
            assert document is not None  # the two arrive together
            await self._store_message(connection, asked, document)
            await connection.execute(_DATE_CONVERSATION, run.conversation_id, now)
        else:
            # A regeneration appends nothing and answers a question that is
            # already stored, so the question is read rather than written --
            # and the conversation is dated all the same, because something
            # happened in it.
            role = await connection.fetchval(
                "SELECT role FROM messages WHERE id = $1 AND conversation_id = $2",
                run.message_id,
                run.conversation_id,
            )
            if role is None:
                raise MessageNotFoundError(
                    f"message {run.message_id} is not in conversation {run.conversation_id}"
                )
            if role != Role.USER.value:
                raise InvalidValueError(
                    f"a run answers a question; message {run.message_id} is a {role} message"
                )
            await connection.execute(_TOUCH_CONVERSATION, run.conversation_id, now)
        try:
            await connection.execute(_INSERT_RUN, *_run_values(run))
        except asyncpg.exceptions.IntegrityConstraintViolationError as violation:
            _refused(
                violation,
                run_id=run.id,
                conversation_id=run.conversation_id,
                message_id=run.message_id,
            )

    async def run_by_id(self, run_id: uuid.UUID) -> Run | None:
        row = await self._pool.fetchrow(f"SELECT {_RUN_COLUMNS} FROM runs WHERE id = $1", run_id)
        return None if row is None else _run(row)

    async def active_run_of(self, conversation_id: uuid.UUID) -> Run | None:
        row = await self._pool.fetchrow(_ACTIVE_RUN, conversation_id, list(ACTIVE_STATES))
        return None if row is None else _run(row)

    async def runs_of(
        self, conversation_id: uuid.UUID, *, limit: int | None = None
    ) -> tuple[Run, ...]:
        if limit is not None:
            _page(limit)
        rows = await self._pool.fetch(
            f"""
            SELECT {_RUN_COLUMNS} FROM runs WHERE conversation_id = $1
            ORDER BY created_at DESC, id DESC
            LIMIT $2::bigint
            """,
            conversation_id,
            limit,
        )
        return tuple(_run(row) for row in rows)

    async def runs_in(
        self, states: Collection[RunState], *, limit: int = MAX_SWEPT
    ) -> tuple[Run, ...]:
        wanted = _states(states)
        _bounded(limit)
        rows = await self._pool.fetch(
            f"""
            SELECT {_RUN_COLUMNS} FROM runs WHERE state = ANY($1::text[])
            ORDER BY created_at, id
            LIMIT $2::bigint
            """,
            sorted(state.value for state in wanted),
            limit,
        )
        return tuple(_run(row) for row in rows)

    async def update_run(self, run: Run) -> None:
        _typed(run, Run, "a run")
        if run.state in ENDED_RUN_STATES:
            raise InvalidValueError(
                f"a run ends through end_run, which writes the event that says so;"
                f" {run.id} was offered as {run.state.value}"
            )
        await self._transacted(lambda connection: self._update_run(connection, run))

    async def _update_run(self, connection: asyncpg.Connection, run: Run) -> None:
        _check_run_change(await self._held_run(connection, run.id), run, ending=False)
        await connection.execute(
            "UPDATE runs SET state = $2, started_at = $3, error = $4 WHERE id = $1",
            run.id,
            run.state.value,
            run.started_at,
            run.error,
        )

    async def end_run(self, run: Run, event: RunEvent, event_document: Document) -> None:
        _typed(run, Run, "a run")
        _typed(event, RunEvent, "a run event")
        written = _document(event_document)
        if run.state not in ENDED_RUN_STATES:
            raise InvalidValueError(
                f"end_run ends a run; {run.id} was offered as {run.state.value}"
            )
        if event.run_id != run.id:
            raise InvalidValueError("a run ends with an event of its own")
        if not isinstance(event.event, RunEnded):
            raise InvalidValueError(
                f"a run ends with the event that says so, not with {event.event!r}"
            )
        if event.event.state is not run.state:
            raise InvalidValueError(
                f"run {run.id} ended {run.state.value} and the event announces"
                f" {event.event.state.value}"
            )
        await self._transacted(lambda connection: self._end_run(connection, run, event, written))

    async def _end_run(
        self, connection: asyncpg.Connection, run: Run, event: RunEvent, document: str
    ) -> None:
        _check_run_change(await self._held_run(connection, run.id), run, ending=True)
        await self._check_position(connection, event)
        await connection.execute(
            "UPDATE runs SET state = $2, started_at = $3, finished_at = $4, error = $5"
            " WHERE id = $1",
            run.id,
            run.state.value,
            run.started_at,
            run.finished_at,
            run.error,
        )
        await self._store_event(connection, event, document)

    # Events.

    async def append_event(self, event: RunEvent, document: Document) -> None:
        _typed(event, RunEvent, "a run event")
        written = _document(document)
        await self._transacted(lambda connection: self._append_event(connection, event, written))

    async def _append_event(
        self, connection: asyncpg.Connection, event: RunEvent, document: str
    ) -> None:
        await self._held_run(connection, event.run_id, ended_is_refused=True)
        await self._check_position(connection, event)
        await self._store_event(connection, event, document)

    async def complete_message(
        self,
        message: Message,
        document: Document,
        event: RunEvent,
        event_document: Document,
        *,
        now: datetime,
    ) -> None:
        _typed(message, Message, "a message")
        _typed(event, RunEvent, "a run event")
        written = _document(document)
        announcement = _document(event_document)
        _instant(now, "now")
        _check_completion(message, event)
        await self._transacted(
            lambda connection: self._complete_message(
                connection, message, written, event, announcement, now
            )
        )

    async def _complete_message(
        self,
        connection: asyncpg.Connection,
        message: Message,
        document: str,
        event: RunEvent,
        event_document: str,
        now: datetime,
    ) -> None:
        # The conversation first and the run second, which is the order every
        # writing method here takes them in.
        if await connection.fetchrow(_LOCK_CONVERSATION, message.conversation_id) is None:
            raise ConversationNotFoundError(
                f"there is no conversation {message.conversation_id} to append to"
            )
        run = await self._held_run(connection, event.run_id, ended_is_refused=True)
        if run.conversation_id != message.conversation_id:
            raise InvalidValueError(
                f"run {run.id} is in conversation {run.conversation_id} and the message"
                f" is in {message.conversation_id}"
            )
        await self._check_position(connection, event)
        await self._store_message(connection, message, document)
        await connection.execute(_DATE_CONVERSATION, message.conversation_id, now)
        await self._store_event(connection, event, event_document)

    async def events_of(
        self, run_id: uuid.UUID, *, after: int = 0, upto: int | None = None
    ) -> tuple[Document, ...]:
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise InvalidValueError(f"a position to read past is a whole number, not {after!r}")
        if upto is not None and (isinstance(upto, bool) or not isinstance(upto, int) or upto < 0):
            raise InvalidValueError(f"a position to read up to is a whole number, not {upto!r}")
        # Two statements rather than one with a ``$3 IS NULL OR`` in it: the
        # planner sees a plain range on the primary key either way, and the
        # unbounded read is what almost every caller makes.
        if upto is None:
            rows = await self._pool.fetch(
                "SELECT document FROM run_events WHERE run_id = $1 AND seq > $2 ORDER BY seq",
                run_id,
                after,
            )
        else:
            rows = await self._pool.fetch(
                "SELECT document FROM run_events"
                " WHERE run_id = $1 AND seq > $2 AND seq <= $3 ORDER BY seq",
                run_id,
                after,
                upto,
            )
        return tuple(_read(row["document"]) for row in rows)

    async def last_position(self, run_id: uuid.UUID) -> int:
        return await self._pool.fetchval(_LAST_POSITION, run_id)

    # What the writing methods are built out of.

    async def _transacted[T](self, work: Callable[[asyncpg.Connection], Awaitable[T]]) -> T:
        """Run ``work`` as one transaction, again if the server says to.

        A deadlock and a serialization failure both mean "this transaction
        did nothing; ask again", so they are the only two retried -- and
        retrying is safe precisely because they rolled back. Everything else,
        a refusal of ours included, comes straight out.
        """
        for attempt in range(MAX_ATTEMPTS):
            try:
                async with self._pool.acquire() as connection, connection.transaction():
                    return await work(connection)
            except (
                asyncpg.exceptions.DeadlockDetectedError,
                asyncpg.exceptions.SerializationError,
            ) as again:
                if attempt == MAX_ATTEMPTS - 1:
                    raise
                _log.warning(RETRIED, type(again).__name__)
        raise AssertionError("unreachable: the last attempt either returns or raises")

    @staticmethod
    async def _held_run(
        connection: asyncpg.Connection, run_id: uuid.UUID, *, ended_is_refused: bool = False
    ) -> Run:
        """That run, its row held until the transaction ends.

        Held, so that reading its state and its last position and then
        writing is one decision: nothing else writes this run meanwhile.
        """
        row = await connection.fetchrow(_LOCK_RUN, run_id)
        if row is None:
            raise RunNotFoundError(f"there is no run {run_id}")
        found = _run(row)
        if ended_is_refused and found.state in ENDED_RUN_STATES:
            raise IllegalTransitionError(
                f"run {found.id} is {found.state.value} and takes no more events"
            )
        return found

    @staticmethod
    async def _check_position(connection: asyncpg.Connection, event: RunEvent) -> None:
        """That this is the next position of that run, the run's row being held.

        One index lookup, whatever the run has published: see ``_LAST_POSITION``
        for why nothing here may read the events themselves. That the run has
        not ended is the caller's, decided from the row it is holding.
        """
        last = await connection.fetchval(_LAST_POSITION, event.run_id)
        if event.seq != last + 1:
            raise PositionTakenError(
                f"run {event.run_id} is at position {last};"
                f" the next event is {last + 1}, not {event.seq}"
            )

    @staticmethod
    async def _store_event(connection: asyncpg.Connection, event: RunEvent, document: str) -> None:
        try:
            await connection.execute(
                _INSERT_EVENT,
                event.run_id,
                event.seq,
                type(event.event).__name__,
                document,
            )
        except asyncpg.exceptions.IntegrityConstraintViolationError as violation:
            _refused(violation, run_id=event.run_id, seq=event.seq)


# The records, out of the columns they were stored in.


def _conversation(row: asyncpg.Record) -> Conversation:
    with reading_stored("a stored conversation cannot be read by this build"):
        return Conversation(
            id=row["id"],
            owner_id=row["owner_id"],
            agent=row["agent"],
            model=row["model"],
            title=row["title"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def _run(row: asyncpg.Record) -> Run:
    with reading_stored("a stored run cannot be read by this build"):
        return Run(
            id=row["id"],
            conversation_id=row["conversation_id"],
            message_id=row["message_id"],
            agent=row["agent"],
            engine=Engine(row["engine"]),
            model=row["model"],
            state=RunState(row["state"]),
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            error=row["error"],
        )


def _conversation_values(conversation: Conversation) -> tuple[object, ...]:
    return (
        conversation.id,
        conversation.owner_id,
        conversation.agent,
        conversation.model,
        conversation.title,
        conversation.created_at,
        conversation.updated_at,
    )


def _run_values(run: Run) -> tuple[object, ...]:
    return (
        run.id,
        run.conversation_id,
        run.message_id,
        run.agent,
        run.engine.value,
        run.model,
        run.state.value,
        run.created_at,
        run.started_at,
        run.finished_at,
        run.error,
    )


# The checks that need nothing but the records in front of them, which is why
# they run before a connection is taken and before anything is written.


def _check_start(conversation: Conversation | None, asked: Message | None, run: Run) -> None:
    """Everything about a turn the records alone can settle."""
    if not run.is_active:
        raise InvalidValueError(f"a run begins active; {run.id} was offered as {run.state.value}")
    if conversation is not None:
        if conversation.id != run.conversation_id:
            raise InvalidValueError("a run and the conversation it begins name one another")
        if run.agent != conversation.agent:
            raise InvalidValueError(
                f"conversation {run.conversation_id} is bound to agent {conversation.agent!r},"
                f" and the run names {run.agent!r}"
            )
    if asked is None:
        if conversation is not None:
            # A conversation being created has no stored message for a run to
            # answer, so a turn that creates one brings its question with it.
            raise MessageNotFoundError(
                f"message {run.message_id} is not in conversation {run.conversation_id}"
            )
        return
    if asked.conversation_id != run.conversation_id:
        raise InvalidValueError("a run and the message it answers name one conversation")
    if run.message_id != asked.id:
        raise InvalidValueError("a run answers the message it is given")
    if asked.role is not Role.USER:
        raise InvalidValueError(
            f"a run answers a question; message {asked.id} is a {asked.role.value} message"
        )


def _check_completion(message: Message, event: RunEvent) -> None:
    """That the message and its announcement say one thing. Records only.

    What is left -- that the run is in the message's conversation -- needs the
    run's row and is checked with it held.
    """
    if not isinstance(event.event, MessageCompleted):
        raise InvalidValueError(
            f"a message completes with the event that says so, not with {event.event!r}"
        )
    if event.event.message.id != message.id:
        raise InvalidValueError(
            f"the event announces message {event.event.message.id}, not {message.id}"
        )
    if message.role is Role.USER:
        raise InvalidValueError(
            f"a run completes answers and tool messages; message {message.id} is a question"
        )
    if message.role is Role.TOOL:
        # A tool message is the platform's own and carries no provenance
        # (``domain.Message``); the run it belongs to is the event's.
        return
    if message.provenance is None or message.provenance.run_id != event.run_id:
        raise InvalidValueError(
            f"message {message.id} does not record run {event.run_id} as what produced it"
        )


def _check_run_change(found: Run, run: Run, *, ending: bool) -> None:
    """That this record may replace the stored one.

    A run still going may change **its state, its start and its error**. Its
    end is ``end_run``'s to write, so ``ending`` says whether ``finished_at``
    is one of the fields allowed to differ; everything else -- the id, the
    conversation, the agent, the engine, the model, the message it answers,
    when it was created -- is what the run is.
    """
    if found.state in ENDED_RUN_STATES:
        raise IllegalTransitionError(
            f"run {run.id} is {found.state.value} and is not written again"
        )
    allowed = replace(
        found,
        state=run.state,
        started_at=run.started_at,
        error=run.error,
        **({"finished_at": run.finished_at} if ending else {}),
    )
    if allowed != run:
        raise InvalidValueError(
            f"run {run.id} may change its state, its start and its error"
            + (", and its end" if ending else "")
            + ", and nothing else"
        )


# Arguments, documents, times, cursors.


def _typed[T](value: object, kind: type[T], what: str) -> T:
    """``value`` if it is the record this method stores."""
    if not isinstance(value, kind):
        raise InvalidValueError(f"{what} is stored, not {value!r}")
    return value


def _document(document: object) -> str:
    """``document`` as the JSON a ``jsonb`` column is given; nothing is read inside it.

    A shallow ``dict`` because ``json`` writes an object for a ``dict`` and
    for nothing else, and the port's shape is a ``Mapping``. What is inside is
    the format's, and is written exactly as it is -- **if it can be**. The
    promise this store makes is that a document comes back equal to what it
    was given, and there are three ways a caller can hand it one that cannot:

    - **a key that is not a string.** ``json`` coerces ``1``, ``True`` and
      ``None`` into ``"1"``, ``"true"`` and ``"null"`` without a word, and a
      mapping holding both ``1`` and ``"1"`` comes back with one of them
      gone. That is the dangerous one: it is silent, and it loses data.
    - **a value JSON has no shape for** -- a set, a datetime, a record. That
      is a ``TypeError`` out of ``json``, and a ``TypeError`` from a store is
      not an answer the port names.
    - **NaN or an infinity.** ``json`` writes them as bare ``NaN`` and
      ``Infinity``, which are not JSON; PostgreSQL refuses them, so the
      caller would meet a driver error about its own value.

    All three are the caller handing this store something it cannot keep, so
    all three are ``InvalidValueError`` and none of them reaches the server.
    ``allow_nan=False`` covers the third, ``json``'s own ``TypeError`` the
    second, and the walk below the first -- which ``json`` will not do for us
    at any setting.
    """
    if not isinstance(document, Mapping):
        raise InvalidValueError(f"a document is a mapping, not {document!r}")
    _string_keys(document)
    try:
        return json.dumps(dict(document), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as cause:
        # ValueError is what `allow_nan=False` raises; TypeError is a value
        # of a kind JSON has no shape for; RecursionError is what a document
        # nested deeper than the interpreter will walk does to the writer.
        raise InvalidValueError(
            "a document holds what JSON can be written from, and this one does not"
        ) from cause


def _string_keys(document: Mapping[object, object]) -> None:
    """Refuse a key that is not a string, at any depth.

    Iterative, because a document is data from outside and a recursive walk
    of a deeply nested one is a ``RecursionError`` where a refusal belongs.
    Only mappings and lists are walked: everything else is a leaf, and what
    ``json`` will not write of a leaf is its own to say, above.
    """
    pending: list[object] = [document]
    while pending:
        found = pending.pop()
        if isinstance(found, Mapping):
            for key, value in found.items():
                if not isinstance(key, str):
                    raise InvalidValueError(
                        f"a document is keyed by text; {key!r} is not, and writing it"
                        " would quietly rename it and could merge two keys into one"
                    )
                pending.append(value)
        elif isinstance(found, list | tuple):
            pending.extend(found)


def _read(document: str) -> Document:
    """A stored document, back as the mapping it was given as.

    A fresh object every time, which is what "neither the caller's mapping nor
    the one handed back is the stored one" means for a store over a database:
    the round trip through the server is the copy.
    """
    with reading_stored("a stored document cannot be read by this build"):
        return json.loads(document)


def _instant(when: datetime, what: str) -> datetime:
    """``when`` if it names an instant; ``InvalidValueError`` if it does not.

    asyncpg would take a naive datetime for a ``timestamptz`` parameter and
    read it as UTC, so a clock that lost its time zone would date every
    conversation by the deployment's offset and say nothing.
    """
    if not isinstance(when, datetime) or when.tzinfo is None:
        raise InvalidValueError(f"{what} must be an aware datetime, not {when!r}")
    return when


def _page(limit: object) -> int:
    """``limit`` if a page may be that long; ``InvalidValueError`` if not."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE:
        raise InvalidValueError(f"a page holds between 1 and {MAX_PAGE} rows")
    return limit


def _bounded(limit: object) -> int:
    """``limit`` if it is a bound this store will read to; refused if not."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_SWEPT:
        raise InvalidValueError(f"at most {MAX_SWEPT} runs are read at once, not {limit!r}")
    return limit


def _states(states: object) -> frozenset[RunState]:
    """``states`` as the set of states to look for."""
    if isinstance(states, str) or not isinstance(states, Collection):
        raise InvalidValueError(f"states to look for are a collection, not {states!r}")
    wanted = frozenset(states)
    if not all(isinstance(state, RunState) for state in wanted):
        raise InvalidValueError(f"a run's states are RunStates, not {states!r}")
    return wanted  # type: ignore[return-value]


def _spelt(conversation: Conversation) -> str:
    """The cursor that means "everything after this one".

    The same spelling the in-memory fake writes, and for the same reason: it
    is a **position inside the caller's own listing** and nothing else. It is
    not signed and does not have to be -- carried to somebody else it moves a
    window over their conversations and can widen it onto nobody's.
    """
    return f"{conversation.updated_at.isoformat()}|{conversation.id}"


def _cursor(cursor: str | None) -> tuple[datetime, uuid.UUID] | None:
    """A cursor as a position; ``InvalidCursorError`` if it does not parse.

    It comes back from a browser, so it is parsed the way anything from
    outside is: every failure is one refusal, and nothing about it reaches a
    query -- the two halves go in as a time and a uuid parameter, never as
    text in a statement.
    """
    if cursor is None:
        return None
    if not isinstance(cursor, str):
        raise InvalidCursorError(f"a cursor is text, not {cursor!r}")
    when, _, which = cursor.partition("|")
    try:
        at = datetime.fromisoformat(when)
        found = uuid.UUID(which)
    except ValueError as cause:
        raise InvalidCursorError("that is not a cursor this store wrote") from cause
    if at.tzinfo is None:
        raise InvalidCursorError("that is not a cursor this store wrote")
    return (at, found)


class _Wording(dict[str, object]):
    """What a refusal's wording is filled from; a name nobody passed is left out."""

    def __missing__(self, key: str) -> str:
        return "?"


def _refused(
    violation: asyncpg.exceptions.IntegrityConstraintViolationError, **named: object
) -> None:
    """Raise the refusal that constraint stands for, or let the violation out.

    Told apart by the constraint's **name**: the class says only "some
    constraint in this statement", and the day a second one of the same kind
    is added to a table every violation of it would be reported as the first.
    """
    answer = CONVERSATION_REFUSALS.get(violation.constraint_name or "")
    if answer is None:
        # A constraint this code has never heard of. Nobody planned to violate
        # it, so it is a bug, and a bug arrives as itself.
        raise violation
    refusal, said = answer
    raise refusal(said.format_map(_Wording(named))) from violation
