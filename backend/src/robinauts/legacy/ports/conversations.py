# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Where conversations, their messages, their runs and a run's events are kept.

**One port, because they are one database and some of the work is one
transaction.** Deleting a conversation removes its runs and its events;
beginning a turn creates a conversation, appends a question and starts a run;
completing a message appends the message and the event announcing it; ending a
run writes the record and the event that says so. Each of those is all of it
or none of it, and two ports could not promise that: a caller that had to
call both would be a place where a process can stop with half the work done.
So this one port owns all four kinds of row, and the operations that must be
indivisible are single methods here rather than sequences of calls up in the
application.

Two shapes cross it, and the difference is the whole of ``docs/layout.md``'s
rule about stores and the platform's format:

- a **conversation** and a **run** cross as records. Their fields are this
  store's own indexed columns -- an owner, an agent, a model, a title, the
  times, a state -- and a store builds one from its columns inside
  ``domain.reading_stored``.
- a **message** and a **run event** cross as **documents**: the plain mapping
  the platform's format is written as, which a store keeps whole and hands
  back untouched, with the record passed beside it for the columns a store
  indexes by (an id, a conversation, a parent, a time, a position). A store
  never parses or builds one -- what a message means is not a store's
  business -- and a document it was given comes back equal to what it was
  given.

**The store keeps no clock.** Every method that has to date something is told
what ``now`` is, by the application, from the ``Clock`` port -- the same one
clock the credential store is held to.

**Nothing here mints an id.** Unlike ``CredentialStore``, which is handed a
person and gives back the user it made for them, this port is handed finished
records: a conversation, a message and a run all name each other, so they are
built together, before any of them is stored, from the ``IdSource`` port.

**Only complete messages are stored as messages.** A message still being
produced lives in its run's events (``docs/specs/conversations.md``,
"Persistence"), so a message stored here is appended once, whole, and never
updated.

**Who checks what.** The store checks everything its own columns and the
domain records in front of it can show: that a row exists, that one row
belongs to another, what role a message has, what state a run is in, that a
position is the next one, and that an event handed to a method is of the kind
that method is for and names the same run, message or state as the record
beside it. It checks none of it by reading a **document**: a document is kept
whole and unread, so "the document says what its record says" is the
application's to keep -- it wrote both -- and so is the shape of a run's
event stream as a whole, which is ``core.check_event_order`` and is exercised
in the tests, not here.

**Every refusal, and its error.**

- a conversation, run, message or event id already stored:
  ``InvalidValueError``. A message id is unique **across the deployment**, as
  a primary key is: the same id offered in another conversation is refused
  too.
- a ``now`` that is not an aware datetime, on every method that takes one:
  ``InvalidValueError``. A naive one names no instant, and the instants
  inside the records are aware already, since the records check themselves.
- a run, a message or an event whose conversation or run is not there:
  ``ConversationNotFoundError``, ``RunNotFoundError``.
- a message whose ``parent_id`` is not a message **of that conversation**:
  ``MessageNotFoundError``. A dangling parent can therefore never be stored,
  whatever a caller believed about the tree.
- a run whose ``message_id`` is not a message of its conversation:
  ``MessageNotFoundError``; one that names a message that is not a
  **question**: ``InvalidValueError``. A run answers a user message.
- a run whose agent is not its conversation's: ``InvalidValueError``. A
  conversation is bound to one agent (``docs/specs/agents.md``).
- a second run while one is going: ``RunAlreadyActiveError``, decided in the
  same step that would have inserted it.
- a run created in a state it has already ended in: ``InvalidValueError``. A
  run begins active; one stored ended, with no events under it, could never
  be made whole and nothing would ever end it.
- an event at a position that is not the next one: ``PositionTakenError``.
- **anything written into a run that has already ended**:
  ``IllegalTransitionError``. A cancelled run does not go on producing
  messages, and a writer that read the last position before the cancel and
  wrote after it would leave a stream nothing can read back.
- a record or an event that does not match what it is written beside -- a
  message completed into another conversation's run, an announcement naming
  another message, an end announcing another state: ``InvalidValueError``.
- an argument outside what the method takes: a ``limit`` outside 1 to
  ``MAX_PAGE`` or to ``MAX_SWEPT``, a position to read past that is negative,
  a cursor that does not parse, a **title** that is not the one bounded,
  printable line a ``Conversation`` holds, a **model** that is not the
  configuration id one holds: ``InvalidValueError``. These are on
  the methods that take them as well, and they are here so that this table is
  what its heading says.

Reading something that is not there is not a refusal: ``conversation_by_id``
and ``run_by_id`` answer ``None``, ``messages_of``, ``events_of`` and
``runs_of`` answer an empty tuple, ``last_position`` answers ``0``,
``conversation_snapshot`` answers an empty snapshot, and the three methods
that change one conversation answer ``None``.

**When more than one of these is true at once, which is raised is not
specified.** A store built on SQL meets them in whatever order its statements
and its constraints happen to, and requiring one order would be requiring an
implementation. What every store owes is that a refused call **writes
nothing**; the contract suite pins a particular error only where exactly one
rule is broken.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from robinauts.legacy.domain import Conversation, Message, Run, RunEvent, RunState

Document = Mapping[str, Any]
"""What the platform's format was written as and a store keeps whole.

A message, or one event of a run. Named here because it is the shape of the
promise, not a convenience: a store that looked inside one would be deciding
what a message means.
"""

MAX_PAGE = 100
"""The most conversations one call may ask for. A panel shows far fewer."""

MAX_SWEPT = 10_000
"""The most runs ``runs_in`` hands back at once: a bound, not a page size.

Nothing here is ever an unbounded read of a growing table. The one caller is
the start-up sweep, which asks for the active states, and a deployment with
more than this many runs still going has something else wrong with it.
"""


@dataclass(frozen=True, slots=True)
class Snapshot:
    """A conversation as it was at **one moment**: the whole of what opening it needs.

    The conversation, every message stored in it, the run in flight if there
    is one, and that run's events -- read together, so that they agree with
    each other. Two reads would not: a message completed between them is in
    neither the tree of the first nor the replay of the second, and whoever
    opened the conversation is shown a gap where an answer is.
    """

    conversation: Conversation | None = None
    messages: tuple[Document, ...] = ()
    active_run: Run | None = None
    events: tuple[Document, ...] = ()
    """The active run's events, all of them, in order of position. Empty when
    no run is going: an ended run's events are read with ``events_of``."""


@dataclass(frozen=True, slots=True)
class ConversationPage:
    """Some of a person's conversations, and how to ask for the next of them."""

    conversations: tuple[Conversation, ...]
    cursor: str | None = None
    """Where the next page begins, or ``None`` when this was the last.

    **A position inside the caller's own listing**, and nothing more. The
    store writes it and the store reads it; it goes out to a browser and comes
    back, so one that does not parse is refused (``InvalidValueError``). One
    that parses and names a position this store never issued is simply a
    position: a listing shows the caller's own conversations whatever the
    cursor says, so there is nothing for a guessed one to reach and nothing
    here is signed.
    """


class ConversationStore(ABC):
    """Durable storage for conversations, messages, runs and run events."""

    # Conversations.

    @abstractmethod
    async def add_conversation(self, conversation: Conversation) -> None:
        """Store a conversation on its own, exactly as it is.

        A turn that begins with a new chat uses ``start_run`` instead, which
        creates the conversation, its first message and its run together. This
        is for a conversation that begins with nothing else. Its id is already
        decided, and one already stored is a bug in the caller rather than
        something to overwrite: ``InvalidValueError``.
        """
        raise NotImplementedError

    @abstractmethod
    async def conversation_by_id(self, conversation_id: uuid.UUID) -> Conversation | None:
        """The conversation with that id, or ``None`` if there is none.

        **It says nothing about who may see it.** Ownership is the
        application's to decide, in one place, so that a conversation of
        somebody else's is answered exactly like one that is not there.
        """
        raise NotImplementedError

    @abstractmethod
    async def conversations_of(
        self, owner_id: uuid.UUID, *, limit: int, cursor: str | None = None
    ) -> ConversationPage:
        """That person's conversations, the most recently updated first.

        Ties are broken by id, so the order is **total**: two conversations
        that share an ``updated_at`` have one order between them, and paging
        past them shows each exactly once. ``limit`` is between 1 and
        ``MAX_PAGE``, and a cursor that does not parse is
        ``InvalidValueError``.

        **That is the whole of what the keyset promises.** A conversation
        *written to* while somebody is paging moves in the order, and moves
        across the cursor with it: one written to after it was passed comes
        back to the top and is not seen again in that pass, and one written to
        before it was reached is seen a second time. Paging walks a list that
        is changing; it is not a transaction over a frozen one, and an
        interface that cares folds by id.

        Nobody else's conversation is ever in the answer, whatever the cursor
        says: the cursor moves a window over this person's listing and cannot
        widen it.
        """
        raise NotImplementedError

    @abstractmethod
    async def rename_conversation(
        self, conversation_id: uuid.UUID, title: str, *, now: datetime
    ) -> Conversation | None:
        """Give it that title and date it ``now``; the conversation as it now is.

        The title is held to what a ``Conversation`` holds -- one bounded,
        printable line (``domain.checked_line``, ``domain.MAX_TITLE_CHARS``) --
        and anything else is ``InvalidValueError`` before anything is written.
        A store that took more would keep a row the record cannot be built
        from, which nobody could read back.

        ``None`` if there was none to rename. It hands back the **written**
        record rather than a ``bool`` so that a caller need not rebuild one out
        of what it read before the write: what it read may be older than what
        is there, and an interface drawn from it would show a title that has
        already changed.
        """
        raise NotImplementedError

    @abstractmethod
    async def set_model(
        self, conversation_id: uuid.UUID, model: str, *, now: datetime
    ) -> Conversation | None:
        """Give it that model and date it ``now``; the conversation as it now is.

        The model's id is held to what a ``Conversation`` holds -- the shape of
        a configuration id (``domain.checked_config_id``) -- and anything else
        is ``InvalidValueError`` before anything is written, as a title is on a
        rename. **Whether the deployment offers that model is not asked
        here**: a store knows no configuration, and the conversation keeps a
        model the operator has since removed anyway (``docs/specs/agents.md``).
        That is the application's check, before it calls this.

        Dated like a rename, because it is the same kind of thing: its author
        changing the conversation, which the listing, most recently updated
        first, should show. ``None`` if there was none, and the **written**
        record otherwise, for the reason ``rename_conversation`` gives.
        """
        raise NotImplementedError

    @abstractmethod
    async def touch_conversation(
        self, conversation_id: uuid.UUID, *, now: datetime
    ) -> Conversation | None:
        """Date it ``now`` and change nothing else; the conversation as it now is.

        What moves a conversation to the top of the list when something
        happened in it that appended no message.
        """
        raise NotImplementedError

    @abstractmethod
    async def delete_conversation(self, conversation_id: uuid.UUID, *, now: datetime) -> bool:
        """Delete it with its messages, its runs and their events; whether there was one.

        **One transaction, and it refuses while a run is going.** Whether the
        conversation is answering is decided *inside* the same step that
        deletes, so a run cannot begin in the window between a check and a
        delete: either the conversation and everything under it are gone, or
        ``RunAlreadyActiveError`` was raised and nothing was touched. Nothing
        can be left orphaned, and no task is left writing messages into a
        conversation that is not there.

        For good: there is no trash in this version
        (``docs/working-notes/poc-scope.md``) and nothing here undoes it.
        ``now`` is taken because every writing method is told the time by the
        one clock; this version records nothing with it, and a trash would
        date the row rather than remove it.
        """
        raise NotImplementedError

    # Messages.

    @abstractmethod
    async def append_message(self, message: Message, document: Document, *, now: datetime) -> None:
        """Append a completed message and date the conversation.

        One step: the document is stored under the message's id and the
        conversation's ``updated_at`` becomes ``now``, because a conversation
        whose newest message is not what dates it is one a panel orders
        wrongly (``docs/specs/conversations.md``).

        ``document`` is the message written in the platform's format and is
        kept **whole and unread**; ``message`` is beside it for the columns
        this store indexes by. The refusals are the ones this module's
        docstring lists: an id already stored, a conversation that is not
        there, a parent that is no message of it.
        """
        raise NotImplementedError

    @abstractmethod
    async def messages_of(self, conversation_id: uuid.UUID) -> tuple[Document, ...]:
        """Every message document of that conversation, oldest first.

        **All of them, ever completed.** A store keeps every message, the
        ones an edit or a regeneration put aside included: the tree is what
        tells the visible path from the rest, and the application builds it
        (``core.ConversationTree``). Only analytics reads the whole of what
        comes back; every other reader is handed the visible path and never a
        message off it. Ordered by ``created_at`` and then by id, so that two
        stores hand back one order.

        The documents are handed back as they were given. A caller may do what
        it likes with what it gets without changing what is stored.
        """
        raise NotImplementedError

    @abstractmethod
    async def conversation_snapshot(self, conversation_id: uuid.UUID) -> Snapshot:
        """Everything opening that conversation needs, as it was at **one moment**.

        The conversation, its message documents, the run in flight if there is
        one, and that run's events -- read together, in one transaction, so
        that they agree.

        **Two reads would not agree**, and the gap is exactly where an answer
        is. A message completed between them is either in the tree and past
        the point the replay starts from, or absent from the tree while the
        replay begins after it: one way the answer is shown twice, the other
        way it is never shown at all and the next announcement hangs under a
        message the client has not got. So this is one method, and a store
        implements it as one snapshot.

        An **ended** run is not in a snapshot: ``active_run`` is ``None`` and
        ``events`` is empty, and an ended run's events are read with
        ``events_of``. So a snapshot taken while a run is ending shows either
        the run with events that do not yet end it, or no run and no events --
        never a run whose events already say it is over.

        A conversation that is not there is an empty ``Snapshot``, whose
        ``conversation`` is ``None``; this says nothing about who may see it.
        """
        raise NotImplementedError

    # Runs.

    @abstractmethod
    async def start_run(
        self,
        *,
        conversation: Conversation | None,
        message: tuple[Message, Document] | None,
        run: Run,
        now: datetime,
    ) -> None:
        """Everything a turn begins with, in one transaction.

        A turn may begin in three shapes, and this is all of them:

        - a **new chat**: ``conversation`` is the one to create, ``message``
          is the first question, ``run`` answers it;
        - a **message in a conversation that exists**: ``conversation`` is
          ``None``, ``message`` is the question, ``run`` answers it;
        - a **regeneration**: both ``conversation`` and ``message`` are
          ``None``; the run answers a question that is already stored.

        All of it happens or none of it does. In particular the refusal of a
        second run is decided in the same step that would have inserted it, so
        two requests arriving together leave one run and one refusal, never
        two answers writing into one conversation.

        **A run answers a question.** ``run.message_id`` is the message given
        here, when one is given, and otherwise a message already stored in
        that conversation; either way it must be a **user** message, which is
        this store's own column. A regeneration naming an answer, or a message
        that is not there, is refused rather than stored as a run that nothing
        can make sense of.

        **A run begins active.** A record already in an ended state is
        refused: it would be a run with no events under it that nothing will
        ever end, and no watcher could be told anything about it.

        Refused with ``RunAlreadyActiveError`` if the conversation is already
        answering, ``ConversationNotFoundError`` if it is not there and none is
        being created, ``MessageNotFoundError`` if the question's parent, or
        the message the run answers, is no message of it, and
        ``InvalidValueError`` if the conversation, the message or the run is
        already stored, if the three do not name each other, if the message
        the run answers is not a question, if the run is not in an active
        state, or if the run's agent is not the conversation's -- a
        conversation is bound to one agent (``docs/specs/agents.md``). A first
        message is appended exactly as ``append_message`` would append it.

        **A turn dates the conversation in every shape**, a regeneration
        included: ``updated_at`` becomes ``now`` even where nothing is
        appended, because something happened in it and a panel ordered by when
        things last happened should say so.
        """
        raise NotImplementedError

    @abstractmethod
    async def run_by_id(self, run_id: uuid.UUID) -> Run | None:
        """The run with that id, or ``None``. It says nothing about who may see it."""
        raise NotImplementedError

    @abstractmethod
    async def active_run_of(self, conversation_id: uuid.UUID) -> Run | None:
        """That conversation's run that is still going, if it has one.

        There is at most one and there cannot be two: ``start_run`` is the
        only way a run begins and it refuses a second, in the step that would
        have made it.
        """
        raise NotImplementedError

    @abstractmethod
    async def runs_of(
        self, conversation_id: uuid.UUID, *, limit: int | None = None
    ) -> tuple[Run, ...]:
        """That conversation's runs, the most recently created first.

        Ties are broken by id, for the reason the listing's are. An empty
        tuple for a conversation with no runs, and for one that is not there.

        ``limit`` is how many of them are wanted, between 1 and ``MAX_PAGE``
        (``InvalidValueError`` outside that); ``None`` is all of them. The
        caller that asks for **one** is opening a conversation, which wants
        nothing but the most recent run and whether it ended badly: a
        conversation answered a thousand times would otherwise be read a
        thousand rows at a time to look at one of them.
        """
        raise NotImplementedError

    @abstractmethod
    async def runs_in(
        self, states: Collection[RunState], *, limit: int = MAX_SWEPT
    ) -> tuple[Run, ...]:
        """The runs of this deployment in one of those states, oldest first.

        What the start-up sweep asks, and the only caller there is: a run left
        ``running`` by a process that went away is marked ``interrupted``
        (``docs/specs/runs.md``). It is asked for the **active** states -- the
        ended ones are the whole history of the deployment and nobody sweeps
        those -- and it is bounded all the same, at ``MAX_SWEPT`` unless a
        caller says less, because a method with no bound over a table that
        only grows is a method waiting to read all of it. ``limit`` outside 1
        to ``MAX_SWEPT`` is ``InvalidValueError``.

        It crosses conversations and owners deliberately -- it is the
        deployment looking at itself, not anybody reading anything.
        """
        raise NotImplementedError

    @abstractmethod
    async def update_run(self, run: Run) -> None:
        """Store this run in place of the one of that id, **without ending it**.

        For a run that is still going and changes state or stamp -- taken up,
        suspended on a tool call, resumed. A run **ends** through ``end_run``,
        which writes the event that says so in the same transaction, so a
        record here that is in an ended state is refused
        (``InvalidValueError``) rather than quietly stored without its event.

        What the store adds is the rule that needs the stored row: **a run
        that has ended is never written again** (``IllegalTransitionError``).
        That is what stops a process coming back from a timeout and re-opening
        a run somebody cancelled.

        Exactly three fields may differ from the stored record: **its state,
        its start and its error**. Its end is ``end_run``'s to write, and its
        id, conversation, agent, engine, model, the message it answers and
        when it was created are what it is; a record that changes any of them
        is ``InvalidValueError``, and one that is not there is
        ``RunNotFoundError``.
        """
        raise NotImplementedError

    @abstractmethod
    async def end_run(
        self,
        run: Run,
        event: RunEvent,
        event_document: Document,
        *,
        engine_state: bytes | None = None,
    ) -> None:
        """Put the run in its ended state and append the event that says so.

        One transaction: a run is never recorded as over without the event
        that announced it, and no watcher is ever told a run ended that the
        record still says is going.

        ``engine_state`` is the conversation as the engine left it after this
        run -- the framework's own serialisation of its history, which the
        platform never reads (``docs/specs/conversations.md``, "The model's
        memory") -- and is stored with the ending, in the same transaction,
        so that a run that finished and the memory it left are one write.
        ``None`` for a run that ended without one, which is every run that
        did not finish. ``engine_state`` reads it back.

        ``run`` is the ended record, and ``event`` carries the announcement at
        its position. **The two must say the same thing**, and the store reads
        the records to see it: the event is a ``RunEnded``, of this run, naming
        the state the record ended in. An announcement of another run, of
        another kind, or of another state is ``InvalidValueError`` and nothing
        is written -- a stream whose last event disagrees with the row is a
        stream a watcher and the conversation read differently.

        The refusals are ``RunNotFoundError`` if the run is not there,
        ``IllegalTransitionError`` if it has already ended,
        ``PositionTakenError`` if the position is not the next one, and
        ``InvalidValueError`` if ``run`` is not in an ended state, changes a
        field it may not (its end is the one ``update_run`` may not write and
        this one must), or does not say what the record says. Which of them a
        call that breaks more than one gets is not specified; what is, is that
        it wrote nothing.
        """
        raise NotImplementedError

    @abstractmethod
    async def engine_state(self, run_id: uuid.UUID) -> bytes | None:
        """The state the engine left after that run, or ``None``.

        ``None`` for a run that ended without one, for one still going, and
        for a run that is not there: the caller walks a conversation's path
        for the nearest finished run and asks this of each candidate, so a
        run without a state is an answer and not a refusal. What comes back
        is the bytes ``end_run`` was given, whole and unread.
        """
        raise NotImplementedError

    # Events.

    @abstractmethod
    async def append_event(self, event: RunEvent, document: Document) -> None:
        """Append one event of a run at its position.

        For every event but the two that come with a row of their own: a
        message completing is ``complete_message`` and a run ending is
        ``end_run``.

        The position must be exactly one past this run's last, which is
        ``FIRST_POSITION`` for the first event. Anything else is
        ``PositionTakenError``, and of several callers offering one position at
        once exactly one is stored. The application is the single writer of a
        run's events and reads the last position from here, so a refusal means
        the run moved on -- somebody ended it -- and not that the numbering
        should be tried again.

        **A run that has ended takes no more events**
        (``IllegalTransitionError``), decided in the same step as the write. A
        writer that read the last position, was cancelled, and wrote
        afterwards would otherwise put an event past the ``RunEnded`` that is
        already there, and nothing could read the stream back.

        ``document`` is the event written in the platform's format, kept whole
        and unread; ``event`` is beside it for the run and the position. A run
        that is not there is ``RunNotFoundError``.
        """
        raise NotImplementedError

    @abstractmethod
    async def complete_message(
        self,
        message: Message,
        document: Document,
        event: RunEvent,
        event_document: Document,
        *,
        now: datetime,
    ) -> None:
        """Append a message a run produced, and the event announcing it, together.

        Everything ``append_message`` does -- the document stored, the
        conversation dated ``now`` and opened on this message -- and the run's
        own announcement of it at ``event``'s position, in one transaction.
        Both or neither: a message stored without its event is a message no
        watcher is told about, and an event without its message is an
        announcement of something that is not there.

        **The four records must agree**, and the store reads them to see it:
        the run is in the message's conversation, the event is a
        ``MessageCompleted`` of that run announcing **this** message, and the
        message is an answer whose provenance names that run. A message
        completed into another conversation's run, or an announcement carrying
        another message, is ``InvalidValueError``.

        A run that has ended completes nothing more
        (``IllegalTransitionError``). Every other refusal of
        ``append_message`` and of ``append_event`` holds here too, and nothing
        is written when one of them is raised.
        """
        raise NotImplementedError

    @abstractmethod
    async def events_of(
        self, run_id: uuid.UUID, *, after: int = 0, upto: int | None = None
    ) -> tuple[Document, ...]:
        """That run's event documents past ``after``, in order of position.

        ``after=0`` is all of them, a position past the end is an empty tuple,
        and a negative one is ``InvalidValueError``. It is the query a watcher
        re-attaches with, and the one opening a conversation with a run in
        flight reads to find where to attach.

        ``upto`` bounds the other end: the events **up to and including** that
        position, and ``None`` is "to the end of the run". It is what a caller
        that needs the beginning of a stream asks with -- the wire's own
        bracketing has to be replayed before a re-attached stream can carry it
        on (``application.Watch.before``) -- and it is a bound of the *query*
        rather than a filter afterwards, so a store answers it with a
        ``seq <= upto`` and reads no row the caller will throw away. A bound
        below ``after`` is an empty tuple, not a refusal: it asks for a slice
        with nothing in it.

        The documents are handed back as they were given; what a caller does
        with them does not reach what is stored.
        """
        raise NotImplementedError

    @abstractmethod
    async def last_position(self, run_id: uuid.UUID) -> int:
        """The position of that run's last event; ``0`` if it has none yet.

        What the application reads before it offers the next one, and what
        whoever marks an orphaned run reads before it appends its end.
        """
        raise NotImplementedError
