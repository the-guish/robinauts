# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The conversation store in dictionaries: what the real one must behave like.

It is the ``ConversationStore`` of ``robinauts.ports`` -- conversations,
messages, runs and their events, since those are one database and some of the
work over them is one transaction -- and it passes the same contract suite the
PostgreSQL store will.

**One lock, held for the whole of an operation.** That is what stands where a
transaction stands in a real store: while it is held nothing else reads or
writes, so a method of the port is indivisible however many dictionaries it
touches. Every public method is a little validation of its arguments, then
``async with self._locked()``, and everything inside it is ordinary
synchronous work on the dictionaries -- no method calls another, so the lock
is never asked for twice.

**The yield inside the lock is deliberate.** Each operation that reads before
it writes awaits ``_a_turn()`` between the two. Under the lock it interleaves
nothing and changes nothing; it is there so that a store *without* the lock is
caught by the contract's concurrency tests instead of passing them because
dictionaries are quick. ``tests/unit/test_fake_conversation_store.py`` has
exactly such a store: this one with ``_locked`` doing nothing, differing in
atomicity and in nothing else, and it fails exactly the tests about two things
at once.

**Documents are copied in and copied out.** A store is not a place where a
caller's mapping lives on: a test that mutated what it appended, or what it
read back, would otherwise be editing the database, and no store over a
database behaves that way. A deep copy in each direction is this store's
version of "it went to jsonb and came back".

**Two active runs in one conversation cannot exist here.** Which one is active
is an index (``_active``), maintained by every write that changes a run's
state, so "at most one" is a property of the data rather than something a read
has to hope for.
"""

from __future__ import annotations

import asyncio
import copy
import uuid
from collections.abc import AsyncIterator, Collection, Mapping
from contextlib import asynccontextmanager
from dataclasses import fields, replace
from datetime import datetime

from robinauts.domain import (
    ENDED_RUN_STATES,
    FIRST_POSITION,
    MAX_TITLE_CHARS,
    Conversation,
    ConversationNotFoundError,
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
    describe,
)
from robinauts.ports import (
    MAX_PAGE,
    MAX_SWEPT,
    ConversationPage,
    ConversationStore,
    Document,
    Snapshot,
)


class MemoryConversationStore(ConversationStore):
    """Conversations, messages, runs and run events in dictionaries."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._conversations: dict[uuid.UUID, Conversation] = {}
        self._documents: dict[uuid.UUID, dict[uuid.UUID, Document]] = {}
        """The message documents of each conversation, by message id."""
        self._messages: dict[uuid.UUID, dict[uuid.UUID, Message]] = {}
        """The records beside them: what a real store's own columns hold."""
        self._runs: dict[uuid.UUID, Run] = {}
        self._states: dict[uuid.UUID, bytes] = {}
        """What the engine left after a finished run, by run id, as it was given."""
        self._active: dict[uuid.UUID, uuid.UUID] = {}
        """Which run of a conversation is going. One id, so there is one run."""
        self._events: dict[uuid.UUID, dict[int, tuple[RunEvent, Document]]] = {}

    # What a test looks at.

    def everything(self) -> str:
        """Every field of every row held, and every document with it.

        What the contract suite reads to say that a deleted conversation left
        nothing behind: a row still there under another key is still a row.
        """
        rows = [_named(conversation) for conversation in self._conversations.values()]
        for conversation_id, documents in self._documents.items():
            records = self._messages[conversation_id]
            for message_id, document in documents.items():
                rows.append(f"{conversation_id}/{message_id}: {document!r}")
                rows.append(f"{conversation_id}/{message_id}: {_named(records[message_id])}")
        rows += [_named(run) for run in self._runs.values()]
        for run_id, events in self._events.items():
            for seq, (event, document) in sorted(events.items()):
                rows.append(f"{run_id}/{seq}: {_named(event)}")
                rows.append(f"{run_id}/{seq}: {document!r}")
        return "\n".join(rows)

    @asynccontextmanager
    async def _locked(self) -> AsyncIterator[None]:
        """The whole of an operation, with nothing else running in it.

        Where a transaction would be. The racy store of the unit tests
        overrides this one method and nothing else.
        """
        async with self._lock:
            yield

    # Conversations.

    async def add_conversation(self, conversation: Conversation) -> None:
        _typed(conversation, Conversation, "a conversation")
        async with self._locked():
            self._check_new_conversation(conversation)
            await _a_turn()
            self._put_conversation(conversation)

    async def conversation_by_id(self, conversation_id: uuid.UUID) -> Conversation | None:
        async with self._locked():
            return self._conversations.get(conversation_id)

    async def conversations_of(
        self, owner_id: uuid.UUID, *, limit: int, cursor: str | None = None
    ) -> ConversationPage:
        _page(limit)
        after = _cursor(cursor)
        async with self._locked():
            ordered = sorted(
                (
                    conversation
                    for conversation in self._conversations.values()
                    if conversation.owner_id == owner_id
                ),
                key=_position,
                reverse=True,
            )
        if after is not None:
            ordered = [conversation for conversation in ordered if _position(conversation) < after]
        page = tuple(ordered[:limit])
        more = len(ordered) > limit
        return ConversationPage(
            conversations=page,
            cursor=_spelt(page[-1]) if more and page else None,
        )

    async def rename_conversation(
        self, conversation_id: uuid.UUID, title: str, *, now: datetime
    ) -> Conversation | None:
        # The rule the record keeps, kept before the write: a store never
        # holds a title `Conversation` would refuse, so neither store may
        # take one.
        checked_line(title, "a conversation's title", MAX_TITLE_CHARS)
        _instant(now)
        async with self._locked():
            found = self._conversations.get(conversation_id)
            await _a_turn()
            if found is None:
                return None
            written = replace(found, title=title, updated_at=now)
            self._conversations[conversation_id] = written
            return written

    async def set_model(
        self, conversation_id: uuid.UUID, model: str, *, now: datetime
    ) -> Conversation | None:
        # Kept before the write, as the title is on a rename.
        checked_config_id(model, "a model's id")
        _instant(now)
        async with self._locked():
            found = self._conversations.get(conversation_id)
            await _a_turn()
            if found is None:
                return None
            written = replace(found, model=model, updated_at=now)
            self._conversations[conversation_id] = written
            return written

    async def touch_conversation(
        self, conversation_id: uuid.UUID, *, now: datetime
    ) -> Conversation | None:
        _instant(now)
        async with self._locked():
            found = self._conversations.get(conversation_id)
            await _a_turn()
            if found is None:
                return None
            written = replace(found, updated_at=now)
            self._conversations[conversation_id] = written
            return written

    async def delete_conversation(self, conversation_id: uuid.UUID, *, now: datetime) -> bool:
        _instant(now)
        async with self._locked():
            found = self._conversations.get(conversation_id)
            going = self._active.get(conversation_id)
            await _a_turn()
            if found is None:
                return False
            if going is not None:
                run = self._runs[going]
                raise RunAlreadyActiveError(
                    f"run {run.id} is {run.state.value} in conversation {conversation_id};"
                    " cancel it before deleting the conversation"
                )
            for run_id in [
                run_id
                for run_id, run in self._runs.items()
                if run.conversation_id == conversation_id
            ]:
                del self._runs[run_id]
                self._events.pop(run_id, None)
                self._states.pop(run_id, None)
            self._documents.pop(conversation_id, None)
            self._messages.pop(conversation_id, None)
            self._active.pop(conversation_id, None)
            del self._conversations[conversation_id]
            return True

    # Messages.

    async def append_message(self, message: Message, document: Document, *, now: datetime) -> None:
        _typed(message, Message, "a message")
        _document(document)
        _instant(now)
        async with self._locked():
            self._check_new_message(message)
            found = self._conversations[message.conversation_id]
            await _a_turn()
            self._put_message(message, document, now, found)

    async def messages_of(self, conversation_id: uuid.UUID) -> tuple[Document, ...]:
        async with self._locked():
            return self._message_documents(conversation_id)

    def _message_documents(self, conversation_id: uuid.UUID) -> tuple[Document, ...]:
        """That conversation's message documents, oldest first. The lock is held."""
        documents = self._documents.get(conversation_id, {})
        records = self._messages.get(conversation_id, {})
        return tuple(
            copy.deepcopy(documents[message.id])
            for message in sorted(records.values(), key=lambda kept: _position(kept, "created_at"))
        )

    def _event_documents(
        self, run_id: uuid.UUID, after: int = 0, upto: int | None = None
    ) -> tuple[Document, ...]:
        """That run's event documents past ``after``, to ``upto``. The lock is held."""
        events = self._events.get(run_id, {})
        return tuple(
            copy.deepcopy(document)
            for seq, (_, document) in sorted(events.items())
            if seq > after and (upto is None or seq <= upto)
        )

    async def conversation_snapshot(self, conversation_id: uuid.UUID) -> Snapshot:
        async with self._locked():
            found = self._conversations.get(conversation_id)
            if found is None:
                return Snapshot()
            # A turn between each read, as between each statement of a store
            # that is not in a transaction. Under the lock they interleave
            # nothing; without it this is where a snapshot tears in two.
            await _a_turn()
            going = self._active.get(conversation_id)
            run = None if going is None else self._runs[going]
            await _a_turn()
            messages = self._message_documents(conversation_id)
            await _a_turn()
            return Snapshot(
                conversation=found,
                messages=messages,
                active_run=run,
                events=() if run is None else self._event_documents(run.id),
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
        if message is not None:
            asked, document = message
            _typed(asked, Message, "a message")
            _document(document)
        _instant(now)
        async with self._locked():
            self._check_start(conversation, asked, run)
            base = (
                conversation
                if conversation is not None
                else self._conversations[run.conversation_id]
            )
            await _a_turn()
            if conversation is not None:
                self._put_conversation(conversation)
            if asked is not None:
                self._put_message(asked, message[1], now, base)  # type: ignore[index]
            else:
                self._conversations[run.conversation_id] = replace(base, updated_at=now)
            self._put_run(run)

    async def run_by_id(self, run_id: uuid.UUID) -> Run | None:
        async with self._locked():
            return self._runs.get(run_id)

    async def active_run_of(self, conversation_id: uuid.UUID) -> Run | None:
        async with self._locked():
            going = self._active.get(conversation_id)
            return None if going is None else self._runs[going]

    async def runs_of(
        self, conversation_id: uuid.UUID, *, limit: int | None = None
    ) -> tuple[Run, ...]:
        if limit is not None:
            _page(limit)
        async with self._locked():
            found = tuple(
                sorted(
                    (run for run in self._runs.values() if run.conversation_id == conversation_id),
                    key=lambda run: _position(run, "created_at"),
                    reverse=True,
                )
            )
        return found if limit is None else found[:limit]

    async def runs_in(
        self, states: Collection[RunState], *, limit: int = MAX_SWEPT
    ) -> tuple[Run, ...]:
        wanted = _states(states)
        _bounded(limit)
        async with self._locked():
            return tuple(
                sorted(
                    (run for run in self._runs.values() if run.state in wanted),
                    key=lambda run: _position(run, "created_at"),
                )[:limit]
            )

    async def update_run(self, run: Run) -> None:
        _typed(run, Run, "a run")
        if run.state in ENDED_RUN_STATES:
            raise InvalidValueError(
                f"a run ends through end_run, which writes the event that says so;"
                f" {run.id} was offered as {run.state.value}"
            )
        async with self._locked():
            self._check_run_change(run, ending=False)
            await _a_turn()
            self._put_run(run)

    async def end_run(
        self,
        run: Run,
        event: RunEvent,
        event_document: Document,
        *,
        engine_state: bytes | None = None,
    ) -> None:
        _typed(run, Run, "a run")
        _typed(event, RunEvent, "a run event")
        _document(event_document)
        if engine_state is not None and not isinstance(engine_state, bytes):
            raise InvalidValueError(f"a framework's state is bytes, not {describe(engine_state)}")
        if run.state not in ENDED_RUN_STATES:
            raise InvalidValueError(
                f"end_run ends a run; {run.id} was offered as {run.state.value}"
            )
        async with self._locked():
            self._check_run_change(run, ending=True)
            self._check_new_event(event)
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
            await _a_turn()
            self._put_run(run)
            if engine_state is not None:
                self._states[run.id] = bytes(engine_state)
            # Two writes with a turn between them, as two statements are. What
            # holds them together is the lock; without it a reader sees one.
            await _a_turn()
            self._put_event(event, event_document)

    async def engine_state(self, run_id: uuid.UUID) -> bytes | None:
        async with self._locked():
            return self._states.get(run_id)

    # Events.

    async def append_event(self, event: RunEvent, document: Document) -> None:
        _typed(event, RunEvent, "a run event")
        _document(document)
        async with self._locked():
            self._check_new_event(event)
            await _a_turn()
            self._put_event(event, document)

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
        _document(document)
        _document(event_document)
        _instant(now)
        async with self._locked():
            self._check_new_message(message)
            self._check_new_event(event)
            self._check_completion(message, event)
            found = self._conversations[message.conversation_id]
            # Three writes -- the message, the conversation it moves, the
            # event -- with a turn between each, as three statements are. What
            # holds them together is the lock; without it a reader falls
            # between them and sees half of a turn.
            await _a_turn()
            self._store_message(message, document)
            await _a_turn()
            self._date_conversation(found, now)
            await _a_turn()
            self._put_event(event, event_document)

    async def events_of(
        self, run_id: uuid.UUID, *, after: int = 0, upto: int | None = None
    ) -> tuple[Document, ...]:
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise InvalidValueError(f"a position to read past is a whole number, not {after!r}")
        if upto is not None and (isinstance(upto, bool) or not isinstance(upto, int) or upto < 0):
            raise InvalidValueError(f"a position to read up to is a whole number, not {upto!r}")
        async with self._locked():
            return self._event_documents(run_id, after, upto)

    async def last_position(self, run_id: uuid.UUID) -> int:
        async with self._locked():
            events = self._events.get(run_id, {})
            return max(events) if events else 0

    # Everything below runs with the lock held and touches no event loop.

    def _check_new_conversation(self, conversation: Conversation) -> None:
        if conversation.id in self._conversations:
            raise InvalidValueError(f"conversation {conversation.id} is already stored")

    def _put_conversation(self, conversation: Conversation) -> None:
        self._conversations[conversation.id] = conversation
        self._documents[conversation.id] = {}
        self._messages[conversation.id] = {}

    def _check_new_message(self, message: Message) -> None:
        if message.conversation_id not in self._conversations:
            raise ConversationNotFoundError(
                f"there is no conversation {message.conversation_id} to append to"
            )
        self._check_free_message_id(message)
        self._check_parent(message)

    def _check_free_message_id(self, message: Message) -> None:
        """That no message of this deployment has that id already.

        Across every conversation, as a primary key is, and not only within
        one: the check belongs to the id, so it runs on the path that creates
        a conversation too, where there is no stored conversation to look in.
        """
        if any(message.id in kept for kept in self._messages.values()):
            raise InvalidValueError(f"message {message.id} is already stored")

    def _check_parent(self, message: Message) -> None:
        if (
            message.parent_id is not None
            and message.parent_id not in self._messages[message.conversation_id]
        ):
            raise MessageNotFoundError(
                f"message {message.parent_id} is not in conversation {message.conversation_id}"
            )

    def _put_message(
        self, message: Message, document: Document, now: datetime, conversation: Conversation
    ) -> None:
        """Store the message and date ``conversation``: the two writes.

        ``conversation`` is the record the operation read, not one read again
        here: a store writes back the row it loaded, and it is the lock around
        the whole operation -- a transaction, in a real one -- that makes
        doing so safe.
        """
        self._store_message(message, document)
        self._date_conversation(conversation, now)

    def _store_message(self, message: Message, document: Document) -> None:
        """The message row alone: the document, and the record beside it."""
        self._documents[message.conversation_id][message.id] = copy.deepcopy(document)
        self._messages[message.conversation_id][message.id] = message

    def _date_conversation(self, conversation: Conversation, now: datetime) -> None:
        """The conversation row alone: dated."""
        self._conversations[conversation.id] = replace(conversation, updated_at=now)

    def _check_start(
        self, conversation: Conversation | None, asked: Message | None, run: Run
    ) -> None:
        if conversation is not None:
            if conversation.id != run.conversation_id:
                raise InvalidValueError("a run and the conversation it begins name one another")
            self._check_new_conversation(conversation)
        elif run.conversation_id not in self._conversations:
            raise ConversationNotFoundError(f"there is no conversation {run.conversation_id}")
        agent = (
            conversation.agent
            if conversation is not None
            else (self._conversations[run.conversation_id].agent)
        )
        if run.agent != agent:
            raise InvalidValueError(
                f"conversation {run.conversation_id} is bound to agent {agent!r},"
                f" and the run names {run.agent!r}"
            )
        if asked is not None:
            if asked.conversation_id != run.conversation_id:
                raise InvalidValueError("a run and the message it answers name one conversation")
            if run.message_id != asked.id:
                raise InvalidValueError("a run answers the message it is given")
            if conversation is None:
                self._check_new_message(asked)
            else:
                # A conversation being created has no messages, so the only
                # parent a first question may have is none -- and its id must
                # still be free of every other conversation's.
                if asked.parent_id is not None:
                    raise MessageNotFoundError(
                        f"message {asked.parent_id} is not in conversation"
                        f" {asked.conversation_id}"
                    )
                self._check_free_message_id(asked)
            self._check_question(asked)
        else:
            if conversation is not None:
                raise MessageNotFoundError(
                    f"message {run.message_id} is not in conversation {run.conversation_id}"
                )
            stored = self._messages[run.conversation_id].get(run.message_id)
            if stored is None:
                raise MessageNotFoundError(
                    f"message {run.message_id} is not in conversation {run.conversation_id}"
                )
            self._check_question(stored)
        self._check_new_run(run)

    @staticmethod
    def _check_question(message: Message) -> None:
        """A run answers a user message, which is this store's own column."""
        if message.role is not Role.USER:
            raise InvalidValueError(
                f"a run answers a question; message {message.id} is a"
                f" {message.role.value} message"
            )

    def _check_new_run(self, run: Run) -> None:
        if run.id in self._runs:
            raise InvalidValueError(f"run {run.id} is already stored")
        if not run.is_active:
            raise InvalidValueError(
                f"a run begins active; {run.id} was offered as {run.state.value}"
            )
        going = self._active.get(run.conversation_id)
        if going is not None:
            other = self._runs[going]
            raise RunAlreadyActiveError(
                f"run {other.id} is {other.state.value} in conversation"
                f" {run.conversation_id}; cancel it or wait for it"
            )

    def _check_run_change(self, run: Run, *, ending: bool) -> Run:
        """The stored run this one replaces, if it may.

        A run still going may change **its state, its start and its error**.
        Its end is written by ``end_run`` alone, so ``ending`` says whether
        ``finished_at`` is one of the fields allowed to differ; everything
        else -- the id, the conversation, the agent, the engine, the model,
        the message it answers, when it was created -- is what the run is.
        """
        found = self._runs.get(run.id)
        if found is None:
            raise RunNotFoundError(f"there is no run {run.id}")
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
        return found

    def _put_run(self, run: Run) -> None:
        self._runs[run.id] = run
        self._events.setdefault(run.id, {})
        if run.is_active:
            self._active[run.conversation_id] = run.id
        elif self._active.get(run.conversation_id) == run.id:
            del self._active[run.conversation_id]

    def _check_new_event(self, event: RunEvent) -> None:
        run = self._runs.get(event.run_id)
        if run is None:
            raise RunNotFoundError(f"there is no run {event.run_id} to append to")
        if run.state in ENDED_RUN_STATES:
            raise IllegalTransitionError(
                f"run {run.id} is {run.state.value} and takes no more events"
            )
        events = self._events[event.run_id]
        expected = (max(events) if events else FIRST_POSITION - 1) + 1
        if event.seq != expected:
            raise PositionTakenError(
                f"run {event.run_id} is at position {expected - 1};"
                f" the next event is {expected}, not {event.seq}"
            )

    def _check_completion(self, message: Message, event: RunEvent) -> None:
        """That the four records say one thing. Columns and records only."""
        run = self._runs[event.run_id]
        if run.conversation_id != message.conversation_id:
            raise InvalidValueError(
                f"run {run.id} is in conversation {run.conversation_id} and the message"
                f" is in {message.conversation_id}"
            )
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
        if message.provenance is None or message.provenance.run_id != run.id:
            raise InvalidValueError(
                f"message {message.id} does not record run {run.id} as what produced it"
            )

    def _put_event(self, event: RunEvent, document: Document) -> None:
        self._events[event.run_id][event.seq] = (event, copy.deepcopy(document))


def _position(record: object, when: str = "updated_at") -> tuple[datetime, bytes]:
    """What a listing is ordered by: a time, and the id that breaks its ties.

    Both halves, always. A single key would let two rows written in one
    millisecond swap places between two pages.
    """
    return (getattr(record, when), record.id.bytes)


def _spelt(conversation: Conversation) -> str:
    """The cursor that means "everything after this one"."""
    return f"{conversation.updated_at.isoformat()}|{conversation.id}"


def _cursor(cursor: str | None) -> tuple[datetime, bytes] | None:
    """A cursor as a position; ``InvalidCursorError`` if it does not parse.

    It comes back from a browser, so it is parsed the way anything from
    outside is: every failure is one refusal, and nothing about it reaches a
    query. A cursor that parses and was never issued is simply a position --
    a listing shows the caller's own conversations whatever it says.
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
    return (at, found.bytes)


def _page(limit: object) -> int:
    """``limit`` if a page may be that long; ``InvalidValueError`` if not.

    The same bound for every listing this store pages -- conversations, and
    the runs of one -- so the refusal names neither.
    """
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


def _typed[T](value: object, kind: type[T], what: str) -> T:
    """``value`` if it is the record this method stores."""
    if not isinstance(value, kind):
        raise InvalidValueError(f"{what} is stored, not {value!r}")
    return value


def _document(document: object) -> Document:
    """``document`` if it is the mapping a store keeps; nothing is read inside it."""
    if not isinstance(document, Mapping):
        raise InvalidValueError(f"a document is a mapping, not {document!r}")
    return document


def _instant(when: datetime) -> datetime:
    """``when`` if it names an instant; ``InvalidValueError`` if it does not."""
    if not isinstance(when, datetime) or when.tzinfo is None:
        raise InvalidValueError(f"now must be an aware datetime, not {when!r}")
    return when


async def _a_turn() -> None:
    """Let the event loop run something else, in the middle of an operation.

    Under the lock it interleaves nothing. It is where a store without one is
    caught: see this module's docstring.
    """
    await asyncio.sleep(0)


def _named(record: object) -> str:
    """Every field of a record, named, whatever its repr chooses to show."""
    return ", ".join(
        f"{field.name}={getattr(record, field.name)!r}"
        for field in fields(record)  # type: ignore[arg-type]
    )
