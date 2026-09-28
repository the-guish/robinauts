# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The run lifecycle: beginning a turn, executing it, cancelling it, sweeping.

A **run** is the work an agent does to answer one user message
(``docs/specs/runs.md``). It is a record in the database, it executes in the
background, and it does not depend on the request that started it: if the tab
closes, the answer is in the conversation when its author comes back. This is
where that happens -- and only here, because everything it does is control
flow over ports: the rules it applies are ``core``'s, the records are
``domain``'s, and nothing outside the process is constructed.

**Beginning and executing are two calls**, deliberately. ``start`` and
``regenerate`` write the whole beginning of a turn in one store call and hand
back the ``Run`` they created; ``execute`` is the coroutine that produces the
answer, and the ``RunExecutor`` port is what carries it in the background.
What a request calls is ``begin`` / ``begin_again``, which are the two
together -- claim the run, hand its work to the executor, answer with the
turn that was started -- so that **nothing outside the application ever
creates a task** and ``execute`` stays a coroutine a test can simply await.
A request is therefore answered as soon as the run exists, and what it then
does -- watch the stream, or drop -- changes nothing about the run.

**Everything stored is announced** through the ``RunSignals`` port, at the
position it was stored at, the moment after it was stored. The announcement
carries nothing: it is how a watcher learns to read again without asking the
database over and over (``application.Watch``), and a signal that is lost
costs a bounded wait and never an event.

**What ``execute`` writes, in order** (``docs/specs/runs.md``, "In the
layout"): the run started, once, first; for each answer, its announcement,
then its text and its thinking as they arrive, then the message itself,
**stored with the event that announces it**; the run ended, once, last. Every
event is numbered from 1 with no gaps by this layer -- an engine knows nothing
of positions -- so one run is numbered one way whichever engine produced it,
and everything stored satisfies ``core.check_event_order``.

**What was published is what was stored.** The engine's fragments go through
``domain.publishable`` / ``flush``, so nothing published ends on half a
character, and an answer whose deltas do not add up to the message it
completed with fails the run rather than leaving a stream nothing can read
back. Reasoning is published as it arrives and **kept in the run's events
alone**: no message holds any of it, so it is in no conversation, is never
sent back to a model and is in no export -- and it is in the events because a
watcher re-attaching in the middle of an answer has to be able to rebuild what
it is watching (``docs/specs/conversations.md``).

**How a turn ends.** The engine returns: the run is ``finished``, unless it
produced no answer or left one announced and never completed, which are
failures (a ``finished`` run has at least one message and nothing
half-written). The engine raises: ``failed``, with a description of what was
raised -- its type and what it said, made storable and bounded by
``core.run_error``, never a traceback -- and the answer in flight is left
uncompleted. The turn takes too long: ``failed``, saying so. The task is
cancelled: ``cancelled``, written under a shield so the ending reaches the
store, and the ``CancelledError`` is **re-raised**, because a coroutine that
swallowed one would leave whoever cancelled it waiting.

**A run that has ended is done with**, and the store says so. If a write is
refused because the run ended under us -- somebody cancelled it, another
process ended it -- ``execute`` stops quietly: the stream is already complete
and correct, and writing past the ``RunEnded`` that is there would leave one
nothing can read.

**Ending a run is one routine, and it is the careful one.** Every way a turn
can end -- finished, failed, timed out, cancelled, interrupted by the sweep,
cancelled from a request with no task here -- goes through ``_ending``, which:

- runs **shielded** from cancellation and is waited on to its end, and only
  then is the cancellation raised again. A cancel landing while the end was
  being written would otherwise leave the run ``running`` for ever with its
  conversation blocked behind it;
- **never trusts a position it only counted**. A write whose await was
  interrupted may have been committed by the store all the same, so the
  position is read back before the end is offered, and a position refused
  while the stored run is still active is read again rather than taken for
  "somebody ended it first". The same holds of every write inside a turn;
- **tries again** a few times when the store cannot be reached, with a short
  wait between. If it still cannot be written, the failure is logged at ERROR
  and **the run is left ``running``**: the start-up sweep of the next restart
  is what ends it. That is a known limit of this version
  (``docs/specs/runs.md``); the periodic sweep that would shorten it is the
  several-processes work the specs describe.

**Cancelling, across the executor boundary.** ``cancel`` marks intent, and
there are two ways for it to reach a run because there are two situations. If
this process is executing it -- ``_executing``, the runs whose ``execute`` has
begun here -- the executor is asked to stop that work, and the task writes the
end a moment later under its shield. Otherwise, which is what a restart looks
like, the run is ended ``cancelled`` in the **store**, with its ``RunEnded``
at the next position. The limits of that are worth saying plainly:

- a run is **claimed** before its work exists (``claim``, then submit, and
  ``let_go`` if the submission was refused), because work that has been
  submitted and not yet begun is a run nothing knows about, and a sweep
  running in that instant would mark it ``interrupted`` while it was about to
  be answered. It is also why the executor is only ever asked to cancel work
  that has **begun**: cancelling work that has not would leave the run
  ``running`` with nothing to write its end;
- the executor's registry is **not** what makes cancelling correct. Losing it
  -- a restart, a second process -- costs the promptness and nothing else: the
  run is ended in the store, and a task still executing it discovers that at
  its next write and stops quietly, because the store refuses everything
  written into an ended run. So the store, which every process shares, is what
  actually stops a run, and the registry is the fast path;
- what it does **not** do is reach into another process to release a provider
  connection there. With several processes that wants a cancellation flag in
  the database and a listener, which is where this grows (``docs/specs/runs.md``,
  "Restarts and several processes"); the POC is one process
  (``docs/working-notes/poc-scope.md``).

**A process that is stopping is not somebody cancelling.** ``stopping`` is how
the composition root says so before it closes the executor: the work of every
run left is cancelled, and each of them ends ``interrupted`` rather than
``cancelled``, because nobody asked for those runs to stop and their authors
may retry them. Nothing is drained (``docs/working-notes/poc-scope.md``); what
the shutdown bound does not cover is ended by the sweep of the next start-up.

**What a turn runs on is the conversation's model, copied onto its run.** A
conversation starts on the model its author picked, or on its agent's
default, and may be moved to another at any point (``set_model``); each run
takes the model the conversation names when the run is begun, and the engine
is handed the **run's**, so a run in flight -- or one taken up again -- keeps
the model it started with (``docs/specs/agents.md``). A model the deployment
no longer offers is refused with ``ModelNotOfferedError`` before anything
is written, and only once the conversation is known to be its caller's: a
model the request names is ``UnknownModelError``.

**Ownership is the same one rule as everywhere else**: a conversation of
somebody else's is answered exactly like one that is not there
(``application.conversations.owner_of``), and a run is reached through its
conversation, so the same holds of a run.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from robinauts.application.conversations import owner_of
from robinauts.core import (
    RUN_ENDED,
    RUN_STARTED,
    ConversationTree,
    derive_title,
    may_transition,
    message_from_stored,
    message_to_data,
    run_event_to_data,
    transition,
    tree_of,
    tree_of_stored,
)
from robinauts.domain import (
    ACTIVE_RUN_STATES,
    FIRST_POSITION,
    AgentDefinition,
    AnswerCompleted,
    AnswerReasoningDelta,
    AnswerStarted,
    AnswerTextDelta,
    Channel,
    Conversation,
    ConversationNotFoundError,
    Engine,
    EngineEvent,
    IllegalTransitionError,
    InvalidValueError,
    Message,
    MessageCompleted,
    MessagePart,
    MessageStarted,
    ModelConfig,
    ModelNotOfferedError,
    PositionTakenError,
    ReasoningDelta,
    Role,
    Run,
    RunAlreadyActiveError,
    RunEnded,
    RunEvent,
    RunNotFoundError,
    RunStarted,
    RunState,
    TextDelta,
    ToolCallArgumentsDelta,
    ToolCallCompleted,
    ToolCallPart,
    ToolCallStarted,
    TurnEvent,
    UnknownAgentError,
    UnknownModelError,
    UnsupportedContentError,
    User,
    WaitingOnTools,
    chain,
    checked_config_id,
    checked_uuid,
    clean_text,
    describe,
    flush,
    kept_parts,
    publishable,
    text_parts,
    where,
)
from robinauts.ports import (
    Agent,
    Clock,
    ConversationStore,
    Document,
    IdSource,
    RunExecutor,
    RunSignals,
)

_log = logging.getLogger(__name__)

DEFAULT_TURN_SECONDS = 600.0
"""How long one turn may take before the run is failed.

Every run has a timeout (``docs/specs/runs.md``). Ten minutes: long enough for
a slow model asked for a long answer, short enough that a provider that
accepted a request and then said nothing does not hold a run ``running`` for
ever. The **per model call** timeout is the engine adapter's, inside the
provider client, and is not this.
"""

TIMED_OUT = "the turn took longer than this deployment allows and was stopped"
"""What a run that ran out of time records. It says what happened, not why."""

NO_ANSWER = "the agent produced no answer"
"""What a turn that yielded nothing records. A turn produces at least one."""

UNFINISHED_ANSWER = "the agent began an answer and never completed it"
"""What a turn that stopped in the middle of an answer without saying so records."""

NO_TOOLS_YET = (
    "the model asked to use a tool, and this deployment runs none yet: give the agent"
    " a model or a system prompt that does not, or wait for the tool loop"
)
"""Why a turn that meets a tool call fails, until the loop that runs one is built.

The engines yield a call where they used to refuse one
(``docs/specs/agents.md``, "Tools"); the application is what does not run it
yet. Failing here, on the first event about a call, is what keeps a stored
answer whole: nothing half-answered is completed, and the run says why.
"""

MAX_WRITE_ATTEMPTS = 3
"""How often one event is offered before the run is failed for it.

An event is offered again only after the **store** has been read and has said
that it is not there: this process lost count of its own numbering, and the
event still has to be written. Three is two more than should ever be needed,
and exhausting them is a fault of its own -- never "somebody ended the run".
"""

UNWRITABLE_STREAM = "the run's stream could not be written"
"""What a run records when its events could not be numbered into the store.

Not "somebody else ended it": nobody did, and a run left ``running`` because
a write kept being refused would be a conversation blocked by a silence.
"""

TWO_WRITERS = "another writer is in this run's stream"
"""What a run records when an event that is not ours stands where ours went.

The application is the single writer of a run's events. If something else is
there, the numbering this process is keeping means nothing, and writing more
would make a stream nobody can read back: the run is failed, saying so.
"""

MAX_ENDING_ATTEMPTS = 3
"""How often ending a run is tried before it is left to the start-up sweep.

Ending a run is the one write with nobody to report a failure to: whoever
started the turn is long gone, and a run left ``running`` blocks its
conversation. So a store that could not be reached is tried again.
"""

ENDING_BACKOFF_SECONDS = 0.05
"""How long the waits between those attempts grow by.

Short: this is a connection that dropped or a lock held for an instant, and
the caller of ``execute`` is a task nobody is waiting on. It is never a retry
policy for a database that is down -- three attempts is a fifth of a second.
"""

ENDING_SECONDS = 10.0
"""How long one attempt at ending a run may take before it is given up on.

A store that refuses is one thing; a store that **never answers** is another,
and without this the task executing a run would wait on it for ever,
uncancellably, and could never be reaped. Each attempt is bounded, and the
attempts are counted.
"""

ENDING_BUDGET_SECONDS = MAX_ENDING_ATTEMPTS * ENDING_SECONDS + ENDING_BACKOFF_SECONDS * sum(
    range(MAX_ENDING_ATTEMPTS)
)
"""How long ending one run may take at the very most: the attempts and the waits.

**One number, added up rather than written down twice.** It is what a process
that is shutting down has to allow for -- a run's ending is written under a
shield, so a shutdown bound shorter than this would abandon writes that were
about to land -- and the composition root is what reads it
(``robinauts.app.SHUTDOWN_SECONDS``). Changing the attempts or their bound
changes this, and changes what a shutdown waits, which is the point of adding
it up here.
"""

QUEUE_DEPTH = 8
"""How many engine events may be ahead of the writer.

The engine is consumed by a task of its own, so that a turn that is over never
waits on one that will not let go; the queue between them is bounded, so that
a provider faster than the database cannot fill memory with an answer nobody
has stored yet. Small: this is back-pressure, not a buffer.
"""

CLOSING_SECONDS = 5.0
"""How long an engine is given to release what it holds once the run is over.

The close runs **after** the run has ended, so this holds up nothing but the
task itself; an engine still not finished after it is abandoned, with a line
in the log.
"""


@dataclass(frozen=True, slots=True)
class StartedTurn:
    """What beginning a turn made: the run, and what it was made in.

    ``conversation`` is the record the turn began in -- the one this call
    created, or the one it was read from a moment before the store dated it.
    The store dated it (``updated_at``); a caller that needs the conversation
    as it now is reads it, rather than being handed a record that was true
    for an instant.
    """

    run: Run
    conversation: Conversation
    message: Message | None = None
    """The question this turn appended; ``None`` for a regeneration, which
    answers a question that is already there."""


class Turns:
    """Beginning, executing, cancelling and sweeping the runs of this deployment."""

    def __init__(
        self,
        *,
        store: ConversationStore,
        clock: Clock,
        ids: IdSource,
        agents: Mapping[str, AgentDefinition],
        models: Mapping[str, ModelConfig],
        engines: Mapping[Engine, Agent],
        executor: RunExecutor,
        signals: RunSignals,
        turn_seconds: float = DEFAULT_TURN_SECONDS,
    ) -> None:
        for model_id, model in models.items():
            if not isinstance(model, ModelConfig) or model.id != model_id:
                raise InvalidValueError(f"the model under {model_id!r} is not that model")
        for agent_id, definition in agents.items():
            if not isinstance(definition, AgentDefinition) or definition.id != agent_id:
                raise InvalidValueError(f"the agent under {agent_id!r} is not that agent")
            if definition.engine not in engines:
                raise InvalidValueError(
                    f"agent {agent_id!r} runs on the {definition.engine.value} engine,"
                    " which this deployment has not wired: point the agent at an engine"
                    " this build runs, or build one and hand it in"
                )
            if definition.model not in models:
                # Every conversation it started would be on a model that is
                # refused at its first turn: said at start-up instead.
                raise InvalidValueError(
                    f"agent {agent_id!r} starts its conversations on model"
                    f" {definition.model!r}, which this deployment does not offer:"
                    " configure the model, or point the agent at one that is"
                )
        if isinstance(turn_seconds, bool) or not isinstance(turn_seconds, int | float):
            raise InvalidValueError(f"a turn's timeout is seconds, not {describe(turn_seconds)}")
        if turn_seconds <= 0:
            raise InvalidValueError("a turn has some time to answer in")
        self._store = store
        self._clock = clock
        self._ids = ids
        self._executor = executor
        self._signals = signals
        # Copied: what a deployment configured is not something a caller goes
        # on editing behind this service's back.
        self._agents = dict(agents)
        self._models = dict(models)
        """The models a conversation may run on: what ``set_model`` accepts and
        what a turn is refused without. Its agents' defaults are among them."""
        self._engines = dict(engines)
        self._turn_seconds = turn_seconds
        self._executing: set[uuid.UUID] = set()
        """Runs whose ``execute`` is under way in this process.

        Ids, not tasks: the work itself is the executor's, and what this is
        for is telling a run being answered here from one that is only
        claimed -- which is what decides whether a cancellation can be handed
        to the executor or has to be written into the store.
        """
        self._claimed: set[uuid.UUID] = set()
        """Runs this process has said it will execute, before their work exists."""
        self._stopping = False
        """Whether the process is shutting down; see ``stopping``."""

    @property
    def agents(self) -> tuple[AgentDefinition, ...]:
        """The agents this deployment is wired with, in configuration order.

        What a picker is drawn from (``docs/specs/agents.md``): a caller that
        needs the list asks for it rather than reaching into the mapping this
        service keeps, which is copied and stays this service's. Each
        definition holds the operator's **system prompt**, so whoever shows
        these sends the fields it means to send and not the record
        (``robinauts.api.schemas.AgentSummary``).
        """
        return tuple(self._agents.values())

    @property
    def models(self) -> tuple[ModelConfig, ...]:
        """The models a conversation of this deployment may run on, in configuration order.

        What the model picker is drawn from, and exactly what ``set_model``
        and a new chat accept, for the reason ``agents`` gives for being a
        tuple rather than the mapping. Each record names its provider and the
        vendor's model, which is the operator's and not a picker's, so
        whoever shows these sends the fields it means to send
        (``robinauts.api.schemas.ModelSummary``).
        """
        return tuple(self._models.values())

    @property
    def executing(self) -> frozenset[uuid.UUID]:
        """The runs this process is executing or has claimed to execute.

        Diagnostics and the start-up sweep, which must interrupt no run of its
        own process -- including one whose task has been created and has not
        yet had its first step, which is why a run is **claimed** before its
        task exists. It is a fast path and never a rule: see this module's
        docstring.
        """
        return frozenset(self._executing) | frozenset(self._claimed) | self._executor.running()

    # --- beginning a turn ----------------------------------------------------

    async def begin(
        self,
        user: User,
        *,
        agent_id: str | None = None,
        model_id: str | None = None,
        conversation_id: uuid.UUID | None = None,
        text: str,
        parent_id: uuid.UUID | None = None,
    ) -> StartedTurn:
        """Begin a turn **and set it going**: what a request asks for.

        ``start`` writes the beginning of the turn and hands back the run;
        this is that, followed by handing the work of the run to the
        ``RunExecutor``. The two are apart because they answer two questions
        -- what was written, and where the work happens -- and they are called
        together by everything outside this service, so that **nothing above
        the application ever creates a task** (``docs/layout.md``).

        It returns as soon as the run exists and the work has been handed
        over. Whoever asked then watches the run or drops; neither changes
        anything about it (``docs/specs/runs.md``).
        """
        started = await self.start(
            user,
            agent_id=agent_id,
            model_id=model_id,
            conversation_id=conversation_id,
            text=text,
            parent_id=parent_id,
        )
        await self._scheduled(started.run)
        return started

    async def begin_again(
        self, user: User, *, conversation_id: uuid.UUID, message_id: uuid.UUID
    ) -> StartedTurn:
        """``regenerate``, with the work of the new run set going. See ``begin``."""
        started = await self.regenerate(
            user, conversation_id=conversation_id, message_id=message_id
        )
        await self._scheduled(started.run)
        return started

    async def _scheduled(self, run: Run) -> None:
        """Claim the run and hand its work over, in that order.

        **Claimed first**, because between the moment work is submitted and
        the moment it begins there is an instant in which nothing at all knows
        the run is being answered, and a sweep looking in that instant would
        mark it ``interrupted`` while an answer was about to be written into
        it.

        A submission the executor refuses -- the process is shutting down, and
        that is the one that happens -- leaves a run in the store that nothing
        will ever execute, so it is **ended here** rather than left going
        until the next start-up: ``interrupted``, because nobody cancelled it
        and its author may retry it. The refusal is then raised, since
        whoever asked for the turn is owed an answer about it.

        **And the same for the instant after the submission was accepted.**
        Work given up on before its first step -- the executor closing in that
        one instant -- runs no line of ``execute``, so nothing writes the run's
        end and nothing lets the claim go; ``never_began`` is what the executor
        runs instead, and it does the same thing here.
        """
        self.claim(run.id)
        try:
            self._executor.submit(
                run.id,
                functools.partial(self.execute, run),
                never_began=functools.partial(self._never_began, run),
            )
        except BaseException as refused:
            self.let_go(run.id)
            _log.error("run %s could not be given to the executor: %s", run.id, chain(refused))
            with contextlib.suppress(Exception):
                await self._end_elsewhere(run, RunState.INTERRUPTED)
            raise

    async def _never_began(self, run: Run, deadline: float) -> None:
        """End a run whose work was given up on before it ever ran.

        The executor calls it, at the latest while it is closing and always
        before the stores are (``robinauts.ports.RunExecutor``). There is
        nothing to cancel and nothing was written: the run is ended
        ``interrupted`` -- the process went away with it, nobody asked for it
        to stop -- and it is given the event that begins it first, as every
        run ended from outside its own task is (``_ending``).

        **``deadline`` is the shutdown's, and it is respected.** The ending is
        written under a shield, which no cancellation can reach, so a report
        that simply waited for it would spend the whole of a slow store's
        budget however short a bound the process was given -- and one report
        per run in flight would be that many times over. When the deadline
        passes the write is **left running** rather than killed: it may be
        about to land, and whether it did is said in the log when it does
        (``_StillWriting.left_writing``). If it does not, the run is ended by
        the start-up sweep of the next restart, which is the limit this
        version has anyway (``docs/specs/runs.md``).

        It lets the claim go **whatever happens**, because a claim left behind
        would hide the run from this process's own start-up sweep.
        """
        try:
            _log.warning(
                "run %s was given up on before its work began; it is ended interrupted", run.id
            )
            await self._end_elsewhere(run, RunState.INTERRUPTED, deadline=deadline)
        except _StillWriting as writing:
            _log.warning(
                "run %s could not be ended inside what was left of the shutdown's bound; the"
                " write was left to finish on its own",
                run.id,
            )
            writing.left_writing(run.id)
            if writing.stopped is not None:  # pragma: no cover -- nothing cancels a report
                raise writing.stopped from None
        finally:
            self.let_go(run.id)

    async def start(
        self,
        user: User,
        *,
        agent_id: str | None = None,
        model_id: str | None = None,
        conversation_id: uuid.UUID | None = None,
        text: str,
        parent_id: uuid.UUID | None = None,
    ) -> StartedTurn:
        """Append a question and begin the run that answers it.

        The two shapes a request with a new message has
        (``docs/specs/wire.md``): an **agent**, which begins a conversation
        with that agent and titles it from the question
        (``core.derive_title``) -- on ``model_id``, or on the agent's default
        when that is ``None``; or a **conversation**, which appends the
        question to it -- under ``parent_id``, which is what makes an edit
        hang beside the message it replaces, putting that message and
        everything under it off the visible path, and a continuation hang
        under the last answer. Nothing for ``parent_id`` is a root: a
        conversation's first question, or another beside it after the first
        was edited.

        All of it is **one** ``start_run``: the conversation if it is new, the
        question, and the run, in one transaction, so nothing can exist
        without the rest of it. The refusal of a second run while one is going
        is decided in that same step and comes back as
        ``RunAlreadyActiveError``.

        The text is repaired on the way in (``domain.clean_text``) and carried
        in as many parts as it needs (``domain.text_parts``); a message with
        nothing in it is refused.

        A model is picked for a conversation that is new, and only then: a
        question in one that exists runs on that conversation's model, which
        is changed by ``set_model`` and never by a turn.
        """
        parts = _asked(text)
        if parent_id is not None:
            checked_uuid(parent_id, "a message's parent id")
        checked_uuid(user.id, "a user's id")
        if agent_id is not None and conversation_id is None:
            return await self._new_chat(user, agent_id, model_id, parts, parent_id)
        if conversation_id is not None and agent_id is None:
            if model_id is not None:
                raise InvalidValueError(
                    "a model is picked when a conversation begins; a turn in one that"
                    " exists runs on the conversation's model"
                )
            return await self._in_conversation(user, conversation_id, parts, parent_id)
        raise InvalidValueError(
            "a turn names the agent to begin a conversation with, or the"
            " conversation it is in, and not both"
        )

    async def regenerate(
        self, user: User, *, conversation_id: uuid.UUID, message_id: uuid.UUID
    ) -> StartedTurn:
        """Answer again the question the named answer's turn began with.

        A regeneration replaces the **turn** (``docs/specs/conversations.md``):
        the new answer hangs under the question that began it, beside the
        answer that was produced before, and not under whatever that answer
        happened to follow. No message is appended -- the question is already
        there -- so the run is the whole of what this writes.
        """
        checked_uuid(message_id, "a message's id")
        checked_uuid(user.id, "a user's id")
        conversation, tree = await self._opened(user, conversation_id)
        question_id = tree.parent_for_regenerate(message_id)
        definition = self._definition(conversation.agent)
        now = self._clock.now()
        run = self._new_run(conversation, question_id, definition, now)
        await self._store.start_run(conversation=None, message=None, run=run, now=now)
        return StartedTurn(run=run, conversation=conversation)

    async def _new_chat(
        self,
        user: User,
        agent_id: str,
        model_id: str | None,
        parts: tuple[MessagePart, ...],
        parent_id: uuid.UUID | None,
    ) -> StartedTurn:
        """A conversation, its first question and the run answering it.

        On the model its author picked, or on the agent's default -- copied
        in, so that what the conversation runs on is read off it alone and an
        operator's later change of the default reaches new conversations only
        (``docs/specs/agents.md``).
        """
        definition = self._definition(agent_id)
        model = self._model(definition.model if model_id is None else model_id)
        conversation_id = self._ids.new_id()
        # A conversation that does not exist yet has no message to hang under,
        # and the rule that says so is the tree's, not one restated here.
        tree_of((), conversation_id=conversation_id).check_attachment(
            parent_id=parent_id, role=Role.USER
        )
        now = self._clock.now()
        message = Message(
            id=self._ids.new_id(),
            conversation_id=conversation_id,
            parent_id=None,
            role=Role.USER,
            parts=parts,
            created_at=now,
            channel=Channel.WEB,
        )
        conversation = Conversation(
            id=conversation_id,
            owner_id=user.id,
            agent=definition.id,
            model=model.id,
            created_at=now,
            updated_at=now,
            title=derive_title((message,)),
        )
        run = self._new_run(conversation, message.id, definition, now)
        await self._store.start_run(
            conversation=conversation,
            message=(message, message_to_data(message)),
            run=run,
            now=now,
        )
        return StartedTurn(run=run, conversation=conversation, message=message)

    async def _in_conversation(
        self,
        user: User,
        conversation_id: uuid.UUID,
        parts: tuple[MessagePart, ...],
        parent_id: uuid.UUID | None,
    ) -> StartedTurn:
        """A question in a conversation that exists, and the run answering it."""
        conversation, tree = await self._opened(user, conversation_id)
        # Whether this parent is a message of **this** conversation, and
        # whether a question may hang under it at all. A parent from somebody
        # else's conversation is simply not among these messages.
        tree.check_attachment(parent_id=parent_id, role=Role.USER)
        definition = self._definition(conversation.agent)
        now = self._clock.now()
        message = Message(
            id=self._ids.new_id(),
            conversation_id=conversation.id,
            parent_id=parent_id,
            role=Role.USER,
            parts=parts,
            created_at=now,
            channel=Channel.WEB,
        )
        run = self._new_run(conversation, message.id, definition, now)
        await self._store.start_run(
            conversation=None,
            message=(message, message_to_data(message)),
            run=run,
            now=now,
        )
        return StartedTurn(run=run, conversation=conversation, message=message)

    async def _opened(
        self, user: User, conversation_id: uuid.UUID
    ) -> tuple[Conversation, ConversationTree]:
        """That conversation, if it is this person's, and its messages as a tree.

        One read of one moment, as opening one is: what a turn decides -- where
        a message may hang, which question a regeneration answers -- is decided
        over messages that were all there together.
        """
        checked_uuid(conversation_id, "a conversation's id")
        snapshot = await self._store.conversation_snapshot(conversation_id)
        conversation = owner_of(user, conversation_id, snapshot.conversation)
        tree = tree_of_stored(
            [message_from_stored(document) for document in snapshot.messages],
            conversation_id=conversation.id,
        )
        return conversation, tree

    def _new_run(
        self,
        conversation: Conversation,
        message_id: uuid.UUID,
        definition: AgentDefinition,
        now: datetime,
    ) -> Run:
        """The run record a turn begins with: active, answering that question.

        No ``started_at``: nothing has taken it up yet. The moment a process
        does is stamped when it ends (``core.transition``), which is where an
        ended run that never recorded a beginning is given one.

        **On the conversation's model**, which the run then keeps however the
        conversation's changes. One the deployment no longer offers is
        ``ModelNotOfferedError`` here, which every way of beginning a turn --
        a question, an edit, a regeneration -- reaches before it writes, and
        after the conversation has been found to be its caller's.
        """
        model = self._models.get(conversation.model)
        if model is None:
            raise ModelNotOfferedError(
                f"conversation {conversation.id} is on model {conversation.model!r},"
                " which this deployment no longer offers"
            )
        return Run(
            id=self._ids.new_id(),
            conversation_id=conversation.id,
            message_id=message_id,
            agent=definition.id,
            engine=definition.engine,
            model=model.id,
            state=RunState.RUNNING,
            created_at=now,
        )

    def _definition(self, agent_id: str) -> AgentDefinition:
        """The agent of that id; ``UnknownAgentError`` if this deployment has none.

        Looked up for every turn, from the definitions this service was wired
        with. **They are fixed at start-up in this version**: the configuration
        is read once by the composition root, so editing an agent takes effect
        at the next turn after a **restart**, rather than at the next turn
        (``docs/specs/agents.md``, where that is a known limit of the POC). The
        lookup is per turn all the same, so the day the definitions are
        reloaded nothing here changes. A conversation bound to an agent the
        operator has since removed meets this too.
        """
        checked_config_id(agent_id, "an agent's id")
        found = self._agents.get(agent_id)
        if found is None:
            raise UnknownAgentError(f"no agent {agent_id!r} is configured in this deployment")
        return found

    def _model(self, model_id: str) -> ModelConfig:
        """The model of that id; ``UnknownModelError`` if this deployment offers none.

        ``domain.ModelsConfig.model_by_id``'s rule, over the models this
        service was wired with -- which are the configuration's in a
        deployment, and whatever a test handed in beside its own agents. For
        a model a request names; a conversation's own is ``_new_run``'s.
        """
        checked_config_id(model_id, "a model's id")
        found = self._models.get(model_id)
        if found is None:
            raise UnknownModelError(f"no model {model_id!r} is configured in this deployment")
        return found

    # --- the conversation's model --------------------------------------------

    async def set_model(
        self, user: User, conversation_id: uuid.UUID, model_id: str
    ) -> Conversation:
        """Move the conversation to another of this deployment's models.

        Here, beside the turns, rather than beside the rename in
        ``application.Conversations``, because what it accepts must be exactly
        what a turn accepts: one service holds the models this deployment
        offers, and it is the one that refuses a turn without one. The
        ownership rule is the same one (``application.conversations.owner_of``).

        A model the deployment does not offer is ``UnknownModelError`` before
        anything is written. **A run in flight is no reason to refuse**: it
        keeps the model it started with, and the change is what the next turn
        runs on (``docs/specs/agents.md``).

        What comes back is what the **store wrote**, for the reason
        ``Conversations.rename`` gives.
        """
        model = self._model(model_id)
        checked_uuid(conversation_id, "a conversation's id")
        checked_uuid(user.id, "a user's id")
        conversation = owner_of(
            user, conversation_id, await self._store.conversation_by_id(conversation_id)
        )
        written = await self._store.set_model(conversation.id, model.id, now=self._clock.now())
        if written is None:
            # Deleted between the read and the write.
            raise ConversationNotFoundError(f"there is no conversation {conversation_id}")
        return written

    # --- executing a turn ----------------------------------------------------

    def claim(self, run_id: uuid.UUID) -> None:
        """Say that this process is about to execute that run.

        **Called before the task exists**, by whoever schedules one: a run
        whose task has been created but has not yet had its first step is a
        run nothing knows about, and a start-up sweep running in that instant
        would mark it ``interrupted`` while it was about to be answered. So the
        order is ``claim(run.id)`` and then create the task; if the task cannot
        be created, ``let_go(run.id)``. ``execute`` claims for itself when
        nobody claimed for it, which is what a test calling it directly does.

        A run this process is already executing or has already claimed is
        ``RunAlreadyActiveError``: one run is answered once.
        """
        checked_uuid(run_id, "a run's id")
        self._refuse_a_second(run_id)
        self._claimed.add(run_id)

    def let_go(self, run_id: uuid.UUID) -> None:
        """Give up a claim whose task was never created. Never fails."""
        self._claimed.discard(run_id)

    async def execute(self, run: Run) -> None:
        """Produce the answer to ``run``, writing everything down as it comes.

        The whole lifecycle, as a coroutine a run executor schedules as a task
        (``docs/specs/runs.md``). It returns when the run has ended and its end
        is stored, or raises ``CancelledError`` having stored that it was
        cancelled -- and **the run has ended either way**, because the ending
        is written by one routine that is shielded from cancellation and reads
        its position back from the store rather than trusting what it counted.
        """
        if not isinstance(run, Run):
            raise InvalidValueError(f"a run is a Run, not {describe(run)}")
        self._take_up(run.id)
        stream = _Stream(self._store, run, self._signals)
        pump = _Pump()
        try:
            ending = await self._turn(stream, pump)
            if ending is not None:
                await self._ended(stream, ending)
        finally:
            # After the ending, never before it: a run that is over must not
            # wait on an engine that is slow to let go. **Letting go of the
            # run happens whatever the release does**, including being
            # cancelled itself: a registry entry left behind would make this
            # process refuse to execute that run ever again, and hide it from
            # its own start-up sweep.
            try:
                await self._released(pump)
            finally:
                self._let_go(run.id)

    async def _turn(self, stream: _Stream, pump: _Pump) -> _Ending | None:
        """Run the turn and decide how it ended; ``None`` if the run ended under us.

        Nothing is written here that ends the run: this decides, ``_ended``
        writes, and there is one writer so that every way a turn can end goes
        through the same shielded, resynchronising routine.
        """
        run = stream.run
        try:
            # The timeout is over the engine and the writes together: what a
            # person is waiting for is the turn, not one call inside it.
            async with asyncio.timeout(self._turn_seconds) as limit:
                answers, unfinished = await self._produce(stream, pump)
        except _Moved:
            # The run ended under us. Its stream is already whole; another
            # event would be one nothing could read back.
            return None
        except asyncio.CancelledError as stop:
            return _Ending(self._stopped_in(), stop=stop)
        except _Faulted as fault:
            # The stream itself could not be written: nobody ended this run
            # and leaving it `running` would block its conversation behind a
            # silence, so it is failed, saying which of the two it was.
            if _being_cancelled():
                return _Ending(self._stopped_in())
            self._failed(run, fault)
            return _Ending(RunState.FAILED, str(fault))
        except TimeoutError as failure:
            if _being_cancelled():
                return _Ending(self._stopped_in())
            if limit.expired():
                return _Ending(RunState.FAILED, TIMED_OUT)
            # A provider's own timeout, not ours: an ordinary failure.
            self._failed(run, failure)
            return _Ending(RunState.FAILED, _described(failure))
        except Exception as failure:
            if _being_cancelled():
                # The turn was cancelled and something raised on the way out
                # -- an engine's `aclose`, a store that lost its connection.
                # The outcome is the cancellation; this is a note in the log.
                self._failed(run, failure, while_cancelled=True)
                return _Ending(self._stopped_in())
            self._failed(run, failure)
            return _Ending(RunState.FAILED, _described(failure))
        if unfinished:
            return _Ending(RunState.FAILED, UNFINISHED_ANSWER)
        if not answers:
            return _Ending(RunState.FAILED, NO_ANSWER)
        return _Ending(RunState.FINISHED)

    async def _produce(self, stream: _Stream, pump: _Pump) -> tuple[int, bool]:
        """Run the engine, publishing and storing what it says; how it ended.

        How many answers it completed, and whether it left one announced and
        never completed -- which is a failure, and is decided by the caller so
        that every ending is written in one place.
        """
        run = await self._taken_up(stream)
        await stream.publish(RunStarted(run_id=run.id, conversation_id=run.conversation_id))
        definition = self._definition(run.agent)
        history = await self._history(run)
        answers = 0
        parent_id = run.message_id
        open_id: uuid.UUID | None = None
        said = _Text()
        thought = _Text()
        # Consumed by a task of its own, through a bounded queue: what the
        # lifecycle awaits is the queue, so a cancellation reaches it at once
        # however long the engine then takes to let go, and a provider faster
        # than the database is held back rather than buffered.
        # The run's model, not the conversation's as it now is: a turn taken
        # up again runs on what it was begun on. No tools yet: the list a run
        # fetches from its agent's servers arrives with the loop that runs them.
        pump.begin(self._engine(run.engine).run_turn(definition, history, (), model=run.model))
        while (event := await pump.next()) is not None:
            if isinstance(event, _TOOL_EVENTS):
                # The engine yielded a call and nothing here runs one yet: the
                # turn fails on the first word of it, so that nothing
                # half-answered is stored (``docs/working-notes/mcp-plan.md``,
                # step 4).
                raise UnsupportedContentError(NO_TOOLS_YET)
            if isinstance(event, AnswerStarted):
                if open_id is not None:
                    raise InvalidValueError("an engine produces one answer at a time")
                open_id = self._ids.new_id()
                said, thought = _Text(), _Text()
                await stream.publish(
                    MessageStarted(
                        run_id=run.id,
                        message_id=open_id,
                        parent_id=parent_id,
                        role=Role.ASSISTANT,
                    )
                )
            elif isinstance(event, AnswerTextDelta):
                open_id = _inside(open_id)
                for piece in said.more(event.text):
                    await stream.publish(TextDelta(run_id=run.id, message_id=open_id, text=piece))
            elif isinstance(event, AnswerReasoningDelta):
                open_id = _inside(open_id)
                for piece in thought.more(event.text):
                    # Published as it arrives and kept in the run's events
                    # alone: no message holds reasoning in this version.
                    await stream.publish(
                        ReasoningDelta(run_id=run.id, message_id=open_id, text=piece)
                    )
            elif isinstance(event, AnswerCompleted):
                open_id = _inside(open_id, completing=True)
                if any(isinstance(part, ToolCallPart) for part in event.parts):
                    raise UnsupportedContentError(NO_TOOLS_YET)
                for piece in said.rest():
                    await stream.publish(TextDelta(run_id=run.id, message_id=open_id, text=piece))
                for piece in thought.rest():
                    await stream.publish(
                        ReasoningDelta(run_id=run.id, message_id=open_id, text=piece)
                    )
                message = await self._complete(stream, open_id, parent_id, event, said)
                answers, parent_id, open_id = answers + 1, message.id, None
            else:
                raise InvalidValueError(f"an engine yields engine events, not {describe(event)}")
        return answers, open_id is not None

    async def _taken_up(self, stream: _Stream) -> Run:
        """Record that a process has taken this run up, before anything else.

        So that a run says when it began while it is still going, rather than
        being given a beginning as it ends (``core.transition``): an operator
        looking at a run in flight can see how long it has been running, and a
        sweep can see which runs are old.

        A run that has ended under us stops the turn here, as everywhere else.
        A stamp that cannot be written for any other reason is **not** worth
        failing a turn for -- the answer matters and the stamp does not -- so
        it is logged and the turn goes on.
        """
        run = stream.run
        if run.started_at is not None:
            return run
        taken = replace(run, started_at=self._clock.now())
        try:
            await self._store.update_run(taken)
        except (IllegalTransitionError, RunNotFoundError) as moved:
            raise _Moved(str(moved)) from moved
        except Exception as failure:
            _log.warning("run %s could not record when it began: %s", run.id, chain(failure))
            return run
        stream.run = taken
        return taken

    async def _complete(
        self,
        stream: _Stream,
        message_id: uuid.UUID,
        parent_id: uuid.UUID,
        completed: AnswerCompleted,
        said: _Text,
    ) -> Message:
        """Store the answer and the event announcing it, in one call.

        The message is the platform's: this id, this parent, the provenance
        the **run** recorded, the clock's time, what this version keeps of
        what the engine produced (``domain.kept_parts`` -- reasoning dropped,
        and one empty piece of text if that leaves nothing), and the vendor's
        ``extras`` as the engine returned them -- stored unread, for the
        adapter that reaches that vendor to replay
        (``docs/specs/conversations.md``, "Reasoning").

        **What was published is what was stored.** An answer that streamed
        text and completed with different text would leave a stream that
        cannot be read back against the conversation, so it fails the run
        instead. An answer that streamed nothing may complete with anything:
        not every provider streams (``docs/specs/agents.md``).
        """
        run = stream.run
        now = self._clock.now()
        message = Message(
            id=message_id,
            conversation_id=run.conversation_id,
            parent_id=parent_id,
            role=Role.ASSISTANT,
            parts=kept_parts(completed.parts),
            created_at=now,
            channel=Channel.WEB,
            provenance=run.provenance,
            extras=completed.extras,
        )
        watched = said.published
        if watched and watched != message.text:
            raise InvalidValueError("an answer's text deltas are the text it completed with")
        await stream.wrote(
            MessageCompleted(run_id=run.id, message=message),
            functools.partial(self._completing, message, message_to_data(message), now),
        )
        return message

    def _completing(
        self, message: Message, document: Document, now: datetime, event: RunEvent
    ) -> Awaitable[None]:
        """The store call that writes an answer and its announcement together."""
        return self._store.complete_message(
            message, document, event, run_event_to_data(event), now=now
        )

    async def _history(self, run: Run) -> tuple[Message, ...]:
        """The whole path down to the question this run answers.

        Read from the store every turn, which is the whole of ADR 0002: the
        conversation record is the state, so a turn needs nothing an earlier
        turn left in memory, and either engine can run it. **Untrimmed**: what
        of it the model sees is the adapter's to decide (ADR 0004), and the
        one thing kept above the port is that the question is in it.
        """
        documents = await self._store.messages_of(run.conversation_id)
        tree = tree_of_stored(
            [message_from_stored(document) for document in documents],
            conversation_id=run.conversation_id,
        )
        return tree.path_to(run.message_id)

    def _engine(self, engine: Engine) -> Agent:
        """The engine of that name, as this deployment wired it."""
        found = self._engines.get(engine)
        if found is None:
            raise InvalidValueError(f"this deployment runs no {engine.value} engine")
        return found

    async def _released(self, pump: _Pump) -> None:
        """Let the engine go: stop the task reading it and close the stream.

        **Bounded, and after the run has ended.** An engine whose ``finally``
        waits on a server that is not answering would otherwise hold a run
        that is already over -- in the task the executor is waiting to reap.
        So the pump is cancelled and **waited for without waiting on it**
        (``asyncio.wait``, which bounds the wait without cancelling this task
        in turn, and which therefore behaves the same whether or not this task
        is itself being cancelled). What has not let go after
        ``CLOSING_SECONDS`` is abandoned with a line in the log, and its
        result is read for it so that a task nobody awaited does not turn into
        a message on the way out of the loop.

        **A cancellation arriving in here is not swallowed.** Whoever sent it
        is waiting for it, so the abandonment is recorded -- the log line and
        the result nobody will read -- and it is raised again. What this must
        never do is return normally from a task that has been cancelled.

        The stream is closed only once nothing is iterating it: closing one
        another task is inside is a bug, not a release.
        """
        task, events = pump.give_up()
        if task is not None:
            if not task.done():
                task.cancel()
            try:
                done, _ = await asyncio.wait({task}, timeout=CLOSING_SECONDS)
            except asyncio.CancelledError:
                self._abandoned(task)
                raise
            if not done:
                self._abandoned(task)
                return
            if not task.cancelled() and task.exception() is not None:
                _log.warning(
                    "an engine failed to release what it held: %s", chain(task.exception())
                )
        if events is None:
            return
        try:
            async with asyncio.timeout(CLOSING_SECONDS):
                await events.aclose()
        except TimeoutError:
            _log.warning("an engine's stream did not close; it was abandoned")
        except asyncio.CancelledError:
            # Somebody cancelled *this*, not the bound: say what was left
            # half-closed and let the cancellation through.
            _log.warning("an engine's stream was left closing when this was cancelled")
            raise
        except Exception as failure:
            _log.warning("an engine failed to release what it held: %s", chain(failure))

    @staticmethod
    def _abandoned(task: asyncio.Task[None]) -> None:
        """Give up on an engine that has not let go, and say so once."""
        _log.warning(
            "an engine did not release what it held within %ss; it was abandoned",
            CLOSING_SECONDS,
        )
        task.add_done_callback(_forgotten)

    # --- ending one --------------------------------------------------------

    async def cancel(
        self, user: User, run_id: uuid.UUID, *, conversation_id: uuid.UUID | None = None
    ) -> Run:
        """Stop the run its author asked to stop; the run as it now is.

        Ownership is the conversation's, so a run in somebody else's
        conversation answers exactly like one that is not there.

        ``conversation_id`` is the conversation the caller says the run is in,
        and is given when the caller was told both -- a URL that names a
        conversation and a run of it (``robinauts.api.conversation_routes``).
        A run of **another** conversation is then answered exactly like a run
        that does not exist: the pair a caller named is either a pair or it is
        nothing, and a mismatch that was quietly ignored would let a run of
        one's own conversation be cancelled through the id of somebody else's,
        or confirm that a run id exists somewhere. It is checked here rather
        than in a route, because it is the same rule as ownership and lives
        beside it.

        A run this process is executing is cancelled by cancelling its task,
        and the task writes the end a moment later: what comes back still says
        ``running``, because asking is not the same as having happened. A run
        no task here is executing -- a restart -- is ended in the store, with
        its ``RunEnded`` at the next position. A run that has already ended is
        not an error: what was asked for is true.
        """
        checked_uuid(run_id, "a run's id")
        checked_uuid(user.id, "a user's id")
        if conversation_id is not None:
            checked_uuid(conversation_id, "a conversation's id")
        run = await self._store.run_by_id(run_id)
        if run is None:
            raise RunNotFoundError(f"there is no run {run_id}")
        if conversation_id is not None and run.conversation_id != conversation_id:
            # The detail says which it was, for the log; what crosses is the
            # one body everything that is not there answers with.
            raise RunNotFoundError(
                f"run {run_id} is a run of conversation {run.conversation_id},"
                f" not of {conversation_id}"
            )
        try:
            # Through its conversation, by the one rule there is.
            owner_of(
                user,
                run.conversation_id,
                await self._store.conversation_by_id(run.conversation_id),
            )
        except ConversationNotFoundError as missing:
            # The request named a run, so it is told about a run -- and told
            # exactly what it would be told if there were no such run at all.
            raise RunNotFoundError(f"there is no run {run_id}") from missing
        if not run.is_active:
            return run
        if run_id in self._executing and self._executor.cancel(run_id):
            # The work is this process's and has begun, so the task it runs
            # in writes the end a moment from now, under its shield.
            return run
        return await self._end_elsewhere(run, RunState.CANCELLED)

    async def sweep_interrupted(self) -> tuple[Run, ...]:
        """End every run left going by a process that is gone; the ones ended.

        For start-up (``docs/specs/runs.md``): a run this process is not
        executing and has not claimed, and that nothing else can be executing
        -- the POC is one process -- is a run whose owner went away, and it is
        marked ``interrupted`` **with the event that ends it**, so that its
        stream is complete and a watcher of one is told it is over rather than
        left waiting.

        A ``waiting`` run holds no process and so is never interrupted; the
        state machine says so and this asks it rather than repeating it. A run
        somebody else ended first is not an error: the store refuses the
        write and this moves on to the next.

        **One run that cannot be ended does not stop the sweep.** This runs at
        start-up, over everything a process that went away left behind; a
        store that would not answer about the third of them must not leave the
        other twenty going. What could not be ended is logged and left for the
        next start-up.

        **What this process has is read again for every run**, after the
        listing: a run claimed while the store was answering is one this
        process is about to execute, and the listing is older than the claim.
        """
        swept: list[Run] = []
        found = await self._store.runs_in(ACTIVE_RUN_STATES)
        for run in found:
            # Read again for every run, and with nothing awaited between the
            # reading and the decision: a run claimed while the listing was
            # being fetched is a run this process is about to answer.
            if run.id in self.executing or not may_transition(run.state, RunState.INTERRUPTED):
                continue
            try:
                ended = await self._end_elsewhere(run, RunState.INTERRUPTED)
            except Exception as failure:
                _log.error("run %s could not be swept: %s", run.id, chain(failure))
                continue
            if ended.state is RunState.INTERRUPTED:
                swept.append(ended)
        return tuple(swept)

    async def _end_elsewhere(
        self, run: Run, state: RunState, *, deadline: float | None = None
    ) -> Run:
        """End a run no task of this process is executing; the run as it now is.

        **Planned from the store, every time.** This process wrote none of the
        events that are there, so it knows nothing about the numbering: each
        attempt reads where the stream is and decides again what is left to
        write -- nothing at all, the event that begins the run and then the one
        that ends it, or only the one that ends it. Reading it once, outside
        the attempts, would make one unreachable moment the end of the whole
        sweep.

        ``deadline`` is how long the **caller** may wait for that, and only a
        process that is shutting down has one: when it passes with the write
        still going, ``_StillWriting`` is raised and the write is left to land
        on its own (``_to_the_end``).
        """
        stream = _Stream(self._store, run, self._signals)
        ended, stopped = await _to_the_end(
            self._ending(stream, state, replan=True), deadline=deadline
        )
        if stopped is not None:
            # Whoever asked was cancelled while this was written; the run
            # ended all the same, and the cancellation is theirs to have.
            raise stopped
        if ended is not None:
            return ended
        # Somebody ended it first; what it says now is what it says.
        return await self._store.run_by_id(run.id) or run

    async def _ended(self, stream: _Stream, ending: _Ending) -> None:
        """Write the end of a run, whatever is happening to this task.

        **Shielded**: a cancellation arriving while the ending is being
        written must not leave the run ``running`` for ever with its
        conversation blocked behind it. The ending is run to completion, and
        only then is the cancellation raised again -- because whoever
        cancelled is waiting for it, and a coroutine that swallowed one would
        leave them waiting.
        """
        _, stopped = await _to_the_end(self._ending(stream, ending.state, error=ending.error))
        stop = ending.stop or stopped
        if stop is None and _being_cancelled():
            stop = asyncio.CancelledError()
        if stop is not None:
            raise stop

    async def _ending(
        self, stream: _Stream, state: RunState, *, error: str | None = None, replan: bool = False
    ) -> Run | None:
        """Put the run in its ended state with the event that says so.

        One store call, so a run is never recorded as over without the
        announcement, and the two say the same thing.

        **It never trusts a position it only counted.** A write whose answer
        never came back may well have been stored, so the position is read
        from the store again before the end is offered, and a position refused
        while the run is still active is read again rather than taken for
        "somebody ended it".

        **A stream that has nothing in it is started first.** A run ended
        before its ``RunStarted`` reached the store -- cancelled in the instant
        after it was created, or left behind by a process that died between
        the two -- would otherwise have a stream beginning with its end, which
        nothing can read back (``core.check_event_order``).

        **A store that cannot be reached, or that does not answer, is tried
        again**, a few times, with a short wait between and each attempt under
        a bound of its own: ending a run is the one write that has nobody to
        report to, and a store that never answers would otherwise hold the
        task for ever where nothing could even cancel it. If it still cannot
        be written, it is logged at ERROR and the run is left ``running`` --
        the start-up sweep of the next restart is what ends it, and that limit
        is in this module's docstring and in ``docs/specs/runs.md``.

        ``None`` when the store refused because the run had already ended,
        which is not a failure: somebody got there first.
        """
        run = stream.run
        unreachable: BaseException | None = None
        for attempt in range(MAX_ENDING_ATTEMPTS):
            if attempt:
                await asyncio.sleep(ENDING_BACKOFF_SECONDS * attempt)
            try:
                async with asyncio.timeout(ENDING_SECONDS):
                    if replan or attempt or not stream.settled:
                        await stream.resync()
                    if not stream.started:
                        await stream.publish(
                            RunStarted(run_id=run.id, conversation_id=run.conversation_id)
                        )
                    ended = transition(run, state, now=self._clock.now(), error=error)
                    await stream.wrote(
                        RunEnded(run_id=run.id, state=state, error=ended.error),
                        functools.partial(self._ending_run, ended),
                    )
                    return ended
            except (_Moved, IllegalTransitionError, RunNotFoundError):
                return None
            except Exception as failure:
                # Including `_Faulted`: an ending that met somebody else's
                # event looks at the store again on the next attempt, which is
                # what re-planning is for.
                unreachable = failure
        return await self._gave_up(run, unreachable)

    async def _gave_up(self, run: Run, unreachable: BaseException | None) -> None:
        """Say what became of a run this process could not end. Always ``None``.

        **Truthfully**, which means looking before speaking: an ending that
        ran out of attempts while something else was writing the stream is not
        a run left going, and saying it stays ``running`` until the next
        restart would be a wrong diagnosis in a log an operator trusts.
        """
        stored: Run | None = None
        with contextlib.suppress(Exception):
            stored = await self._store.run_by_id(run.id)
        if stored is not None and not stored.is_active:
            # Somebody ended it while this was trying to. Nothing to report:
            # the run is over and its stream is whole.
            return None
        # It is still going, whatever went wrong here -- a store that would
        # not answer, a stream that could not be written, another writer in
        # it. **The state decides what is said**, not which of them it was,
        # because what matters to an operator is that a conversation is
        # blocked behind a run nothing is ending.
        _log.error(
            "run %s could not be ended (%s); it is still %s, and a run left running is"
            " ended by the start-up sweep of the next restart",
            run.id,
            chain(unreachable) if unreachable is not None else "no reason given",
            (stored or run).state.value,
        )
        return None

    def _ending_run(self, ended: Run, event: RunEvent) -> Awaitable[None]:
        """The store call that writes an ended run and its announcement together."""
        return self._store.end_run(ended, event, run_event_to_data(event))

    # --- the process's own bookkeeping --------------------------------------

    def _refuse_a_second(self, run_id: uuid.UUID) -> None:
        """``RunAlreadyActiveError`` if this process already has that run.

        Two executions of one run would write two streams into one numbering
        and race each other for every position.
        """
        if run_id in self._claimed or run_id in self._executing:
            raise RunAlreadyActiveError(f"run {run_id} is already being executed by this process")

    def _take_up(self, run_id: uuid.UUID) -> None:
        """Say that this call is the execution of that run.

        A claim made before the work existed is taken up by it, so that the
        run is in ``executing`` without interruption from the moment it was
        claimed to the moment the work lets go. A run this process is already
        executing is refused: one run is answered once here.
        """
        if run_id in self._executing:
            raise RunAlreadyActiveError(f"run {run_id} is already being executed by this process")
        self._claimed.discard(run_id)
        self._executing.add(run_id)

    def _let_go(self, run_id: uuid.UUID) -> None:
        """Forget it, however it was registered. Never fails."""
        self._executing.discard(run_id)
        self._claimed.discard(run_id)

    def stopping(self) -> None:
        """Say that this process is shutting down. Nothing is undone.

        What it changes is the **meaning of a cancellation**: work stopped
        from now on was stopped because the process is going away, so the run
        ends ``interrupted`` rather than ``cancelled`` -- nobody cancelled it,
        it is the author's to retry, and an interface must not tell them they
        stopped something they did not (``docs/specs/runs.md``).

        The composition root calls it at shutdown, before it closes the
        executor, so that every run cancelled by that close is recorded for
        what it was. There is no way back: a process that is stopping stays
        stopping.
        """
        self._stopping = True

    @property
    def is_stopping(self) -> bool:
        """Whether this process has said it is shutting down."""
        return self._stopping

    def _stopped_in(self) -> RunState:
        """What a run whose work was stopped ends in: whose stopping it was.

        Somebody asked for that run to stop: ``cancelled``. The process is
        stopping and took every run with it: ``interrupted``.
        """
        return RunState.INTERRUPTED if self._stopping else RunState.CANCELLED

    def _failed(self, run: Run, failure: BaseException, *, while_cancelled: bool = False) -> None:
        """Write the whole of what went wrong to the log, in one bounded line.

        The record keeps a sentence (``core.run_error``); the log keeps the
        chain of causes and the frames of the innermost one, which is what an
        operator needs and what a sentence cannot hold.

        **Escaped and bounded**, through ``domain.chain`` and ``domain.where``,
        like everything a log line carries that we did not write: an exception
        raised inside a provider's client holds whatever the provider sent,
        newlines and megabytes included, and a traceback written out raw would
        be a record of what happened that somebody else chose the shape of.
        """
        _log.warning(
            "run %s %s: %s at %s",
            run.id,
            "was cancelled and its engine then failed" if while_cancelled else "failed",
            chain(failure),
            where(failure),
        )


_TOOL_EVENTS = (ToolCallStarted, ToolCallArgumentsDelta, ToolCallCompleted, WaitingOnTools)
"""The engine events about tools, which the loop of the next step answers."""


@dataclass(frozen=True, slots=True)
class _Ending:
    """How a turn ended, before any of it is written down."""

    state: RunState
    error: str | None = None
    stop: asyncio.CancelledError | None = None
    """The cancellation to raise again once the run has been ended."""


class _Pump:
    """The engine's stream, read by a task of its own, through a bounded queue.

    **Why a task and not a loop.** An engine is an async generator, and a
    generator's ``finally`` runs wherever the iteration is -- so an engine
    that takes a minute to let go of its connection would take that minute
    *inside* the lifecycle's own loop, while a run that has been cancelled
    waits to be ended. With the iteration in a task of its own, the lifecycle
    awaits a queue: a cancellation reaches it at once, the run is ended, and
    only then is the engine cancelled, waited for briefly and abandoned.

    **Bounded**, because the other thing a task in front would do is buffer:
    a provider faster than the database would fill memory with an answer
    nobody had stored. The queue holds ``QUEUE_DEPTH``, and the engine waits
    for the writer, which is what it did before.

    What the queue carries is one of three things: an event, the end of the
    turn, or what the engine raised -- which the lifecycle raises in its own
    frame, so that a failure is a failure of the turn wherever it was read.
    """

    def __init__(self) -> None:
        self.events: AsyncGenerator[EngineEvent, None] | None = None
        self.task: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[tuple[EngineEvent | None, BaseException | None]] = asyncio.Queue(
            maxsize=QUEUE_DEPTH
        )

    def begin(self, events: AsyncGenerator[EngineEvent, None]) -> None:
        """Start reading that stream."""
        self.events = events
        self.task = asyncio.create_task(self._reading(events))

    async def next(self) -> EngineEvent | None:
        """The next event of the turn, ``None`` when there are no more.

        What the engine raised is raised here, in the lifecycle's own frame.
        """
        event, failure = await self._queue.get()
        if failure is not None:
            raise failure
        return event

    async def _reading(self, events: AsyncGenerator[EngineEvent, None]) -> None:
        """Read the engine into the queue until it ends, fails or is cancelled."""
        try:
            async for event in events:
                await self._queue.put((event, None))
        except asyncio.CancelledError:
            # Ours, and nobody is reading any more: nothing to hand over.
            raise
        except BaseException as failure:  # noqa: BLE001 - handed to the lifecycle
            await self._queue.put((None, failure))
        else:
            await self._queue.put((None, None))

    def give_up(self) -> tuple[asyncio.Task[None] | None, AsyncGenerator[EngineEvent, None] | None]:
        """Hand over the task and the stream, and hold neither any more."""
        task, self.task = self.task, None
        events, self.events = self.events, None
        return task, events


class _Stream:
    """A run's events, numbered as they are written.

    The application is the single writer of a run's events
    (``docs/specs/runs.md``), so the next position is ordinarily known rather
    than read. **Ordinarily** covers neither of the two things that go wrong,
    and both are handled the same way:

    - a write whose answer never came back -- a cancellation or a timeout
      landing as the store commits -- may have been stored all the same;
    - a position refused means **somebody else wrote**, and a single writer
      that assumed otherwise would be wrong about which of the two it was.

    So a refusal and an unknown outcome are the same question -- *what is
    actually stored where I offered?* -- and it is asked of the store, not
    guessed. **An event is never simply said again**: a second ``RunStarted``
    offered at the next free position would make a stream nothing can read
    back, which is exactly what two writers produce when one of them retries
    blindly.
    """

    def __init__(
        self, store: ConversationStore, run: Run, signals: RunSignals, position: int = 0
    ) -> None:
        self._store = store
        self._signals = signals
        self.run = run
        self.position = position
        self._offered: RunEvent | None = None
        """The event whose outcome is not known yet, if there is one."""

    @property
    def settled(self) -> bool:
        """Whether ``position`` is known to be what the store holds."""
        return self._offered is None

    @property
    def started(self) -> bool:
        """Whether anything of this run's stream is stored yet."""
        return self.position >= FIRST_POSITION

    async def resync(self) -> None:
        """Settle what is unknown, then read where this run's stream really is."""
        await self._settled()
        self.position = await self._store.last_position(self.run.id)

    async def publish(self, event: TurnEvent) -> None:
        """Append one event of the run at its position."""
        await self.wrote(event, self._appending)

    def _appending(self, event: RunEvent) -> Awaitable[None]:
        return self._store.append_event(event, run_event_to_data(event))

    async def wrote(self, event: TurnEvent, send: Callable[[RunEvent], Awaitable[None]], /) -> None:
        """Store ``event`` at the next position, looking at the store if it moved.

        ``send`` is given the numbered event, because the store call it makes
        may carry a message or an ended run beside it -- and because an event
        offered again is numbered again rather than offered a position that is
        taken.

        Every refusal and every unknown outcome goes through ``_settled``,
        which **reads what is stored where this offered** and decides: it is
        already there (nothing more to do), the run has ended (``_Moved``),
        somebody else's event is there (``_Faulted``), or it is not there and
        may be offered again from the position the store reports.
        """
        for _ in range(MAX_WRITE_ATTEMPTS):
            if not await self._settled():
                return
            numbered = RunEvent(run_id=self.run.id, seq=self.position + 1, event=event)
            self._offered = numbered
            try:
                await send(numbered)
            except (PositionTakenError, IllegalTransitionError, RunNotFoundError):
                # A refusal wrote nothing; what it means is `_settled`'s to
                # say, on the next turn of this loop.
                continue
            self._offered = None
            self.position = numbered.seq
            await self._announced(numbered)
            return
        raise _Faulted(UNWRITABLE_STREAM)

    async def _announced(self, event: RunEvent) -> None:
        """Say that this run has moved, so that its watchers stop waiting.

        **After the write and never before it**: what an announcement makes
        somebody go and read must already be there to read
        (``robinauts.ports.RunSignals``).

        Nothing it does can fail a write that has already happened. A signal
        that could not be sent costs every watcher of this run one bounded
        wait -- they read the store when it passes, because that is what a
        watcher does with either answer -- so it is logged and the turn goes
        on. A cancellation is not that, and passes through.
        """
        try:
            await self._signals.announce(
                self.run.id, event.seq, ended=isinstance(event.event, RunEnded)
            )
        except asyncio.CancelledError:
            raise
        except Exception as failure:  # noqa: BLE001 - a signal never fails a write
            _log.warning(
                "run %s reached position %d and could not be announced: %s",
                self.run.id,
                event.seq,
                chain(failure),
            )

    async def _settled(self) -> bool:
        """Settle the offered event against the store; whether it is still to write.

        ``True`` when nothing is outstanding, or when what was offered is not
        stored and may be offered again -- ``position`` is then what the store
        reports. ``False`` when it **is** stored, which is what a write whose
        answer never came back looks like from here.

        ``_Moved`` if the run has ended under us, ``_Faulted`` if an event that
        is not ours stands where ours was offered: the application is the
        single writer of a run's events, so anything else there is a fault
        rather than a position to renumber past.
        """
        offered, self._offered = self._offered, None
        if offered is None:
            return True
        standing = await self._store.events_of(self.run.id, after=offered.seq - 1)
        if not standing:
            # Nothing at or past where we offered: it was not stored, and the
            # store says where the stream really is.
            self.position = await self._store.last_position(self.run.id)
            return True
        if _is_ours(offered, standing):
            self.position = offered.seq
            # Stored after all -- a write whose answer never came back. It is
            # stored, so it is announced: "everything stored is announced"
            # must not depend on which of the two ways it got there.
            await self._announced(offered)
            return False
        if any(_is_the_end(document) for document in standing):
            raise _Moved(f"run {self.run.id} has ended")
        raise _Faulted(TWO_WRITERS)


def _is_ours(offered: RunEvent, standing: Sequence[Document]) -> bool:
    """Whether the event that was offered is what is stored where it was offered.

    The documents are compared, because the document is what was written: the
    canonical encoding of an event is a function of the event, so two equal
    documents are one event stored once.

    ``RunStarted`` is the one that is matched more widely -- **any**
    ``RunStarted`` of this run counts -- because two processes may both find a
    run with an empty stream and both decide it needs the event that begins
    it, and a run must not be started twice whichever of them numbered it.
    """
    if isinstance(offered.event, RunStarted):
        return any(_is_the_beginning(document) for document in standing)
    return standing[0] == run_event_to_data(offered)


def _is_the_end(document: Document) -> bool:
    """Whether that stored event document is the one that ends a run."""
    return _kind_of(document) == RUN_ENDED


def _is_the_beginning(document: Document) -> bool:
    """Whether that stored event document is the one that begins a run."""
    return _kind_of(document) == RUN_STARTED


def _kind_of(document: Document) -> object:
    """What kind of event that document holds, without decoding the rest of it.

    A document of ours, read for one field. Anything unexpected in it is not
    this function's to complain about: the readers (``core``) are where a row
    that cannot be read becomes ``StoredDataError``, and here it is simply not
    the kind being looked for.
    """
    event = document.get("event")
    return event.get("kind") if isinstance(event, Mapping) else None


class _Moved(Exception):
    """The run ended under the task executing it. Not a failure of the turn.

    Private, and caught in ``_turn`` before anything else looks at it: it is
    control flow between this module's own methods, and nothing outside sees
    it.
    """


class _Faulted(Exception):
    """The run's stream could not be written, and nobody ended the run.

    The other thing a refused write can mean. It is **not** ``_Moved``: there
    is no ``RunEnded`` anywhere, so stopping quietly would leave a run
    ``running`` with its conversation blocked behind it and nothing said
    anywhere. The turn is failed with what it says
    (``UNWRITABLE_STREAM``, ``TWO_WRITERS``).
    """


class _StillWriting(Exception):
    """A shielded ending outlived the bound the caller was allowed to wait.

    Raised by ``_to_the_end`` when it is given a deadline, and never when it
    is not, so only a shutdown ever meets it. It is **not** a failure of
    anything: the write is still going, uncancelled, and what it carries is
    that write -- ``left_writing`` is how whoever returns says in the log what
    became of it.
    """

    def __init__(
        self, writing: asyncio.Future[Any], stopped: asyncio.CancelledError | None = None
    ) -> None:
        super().__init__("the run's ending is still being written")
        self.writing = writing
        """The ending, still running. Nobody waits on it; somebody logs it."""
        self.stopped = stopped
        """A cancellation that arrived meanwhile and is still owed to whoever sent it."""

    def left_writing(self, run_id: uuid.UUID) -> None:
        """Say in the log what became of that write, whenever it lands.

        A done callback rather than a task to wait on: whoever calls this is
        on its way out under somebody else's bound. It also **retrieves** what
        the write raised, which an unretrieved exception at shutdown would
        otherwise have the loop print in nobody's frame.
        """

        def said(finished: asyncio.Future[Any]) -> None:
            if finished.cancelled():  # pragma: no cover -- nothing cancels it
                _log.error(
                    "run %s was left being ended as the process stopped, and that write was"
                    " cancelled; the start-up sweep of the next restart ends it",
                    run_id,
                )
                return
            failure = finished.exception()
            if failure is not None:
                _log.error(
                    "run %s was left being ended as the process stopped, and it failed:"
                    " %s; the start-up sweep of the next restart ends it",
                    run_id,
                    chain(failure),
                )
            else:
                _log.warning(
                    "run %s was ended after the process had stopped waiting for it", run_id
                )

        self.writing.add_done_callback(said)


def _forgotten(task: asyncio.Task[None]) -> None:
    """Read the result of a task nobody waited for, so the loop says nothing.

    Only ever the engine's pump, and only when it was abandoned for taking too
    long to let go: what it raises then is of no interest, and an unretrieved
    exception would be printed by the loop on its way out.
    """
    if not task.cancelled():
        task.exception()


class _Text:
    """What of an engine's fragments has been published, and what waits.

    ``domain.publishable`` in a place to keep its carry: a character that
    arrived in two halves is held until its other half comes, and what is
    published, joined, is exactly ``clean_text`` of everything the engine
    sent.
    """

    def __init__(self) -> None:
        self._carry = ""
        self._published: list[str] = []

    def more(self, fragment: str) -> tuple[str, ...]:
        """The pieces of ``fragment`` that can be published now."""
        pieces, self._carry = publishable(self._carry, fragment)
        self._published.extend(pieces)
        return pieces

    def rest(self) -> tuple[str, ...]:
        """What is left once there is no more to come: at most one piece."""
        last = flush(self._carry)
        self._carry = ""
        if not last:
            return ()
        self._published.append(last)
        return (last,)

    @property
    def published(self) -> str:
        """Everything published for this answer, joined."""
        return "".join(self._published)


async def _to_the_end[T](
    work: Coroutine[Any, Any, T], *, deadline: float | None = None
) -> tuple[T, asyncio.CancelledError | None]:
    """Run ``work`` to completion however often this task is cancelled meanwhile.

    ``asyncio.shield`` alone does not do this: it keeps the work running and
    hands the caller the cancellation at once, so the caller would return
    before the write it was waiting for had happened. Here the shield is
    waited on again until the work is done, and the cancellation that arrived
    is handed back to be raised afterwards, when there is nothing left to
    lose by raising it.

    **``deadline`` is somebody else's bound, and it wins.** A process that is
    stopping says how long the whole of its shutdown may take
    (``robinauts.ports.RunReport``), and waiting here for a store that is not
    answering would spend that bound however often it was asked not to. When
    the deadline passes with the work still going, ``_StillWriting`` is raised
    and carries the work with it: **the work is not cancelled** -- it may be
    half way through the one write a run's ending is -- so whoever asked is
    the one who returns, and what became of the write is theirs to log when it
    lands. Without a deadline this waits for as long as the work takes, which
    is what every caller but a shutdown wants.
    """
    running = asyncio.ensure_future(work)
    stopped: asyncio.CancelledError | None = None
    while True:
        try:
            if deadline is None:
                return await asyncio.shield(running), stopped
            async with asyncio.timeout_at(deadline):
                return await asyncio.shield(running), stopped
        except asyncio.CancelledError as stop:
            if running.done():
                # It was the work's own cancellation, not one aimed at us.
                raise
            stopped = stop
        except TimeoutError:
            if running.done():
                # The work's own timeout, on its way out of it, and not the
                # deadline above: it is the work's answer and it is raised.
                raise
            raise _StillWriting(running, stopped) from None


def _being_cancelled() -> bool:
    """Whether the task running this has been asked to stop.

    What tells a failure apart from a cancellation that something raised on
    its way out: an engine whose ``aclose`` raises while the turn is being
    cancelled must not turn the run into a failure and swallow the
    cancellation.
    """
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


def _inside(open_id: uuid.UUID | None, *, completing: bool = False) -> uuid.UUID:
    """The answer being produced; ``InvalidValueError`` if there is none.

    An engine that streams or completes without announcing an answer is an
    engine breaking the one order there is (``core.check_engine_events``), and
    a run whose stream nothing can read back is worse than a failed one.
    """
    if open_id is None:
        raise InvalidValueError(
            "an answer is completed once, after it was announced"
            if completing
            else "a delta belongs to an answer being produced"
        )
    return open_id


def _asked(text: object) -> tuple[MessagePart, ...]:
    """What a person wrote, as content: repaired, bounded, and not nothing.

    ``clean_text`` first, because a browser can send what no database will
    hold, and ``text_parts`` after it, because a message longer than one part
    is carried in the next rather than cut.
    """
    if not isinstance(text, str):
        raise InvalidValueError(f"a message is written in text, not {describe(text)}")
    kept = clean_text(text)
    if not kept.strip():
        raise InvalidValueError("a message has something in it")
    return text_parts(kept)


def _described(failure: BaseException) -> str:
    """What a run records of what was raised: its type, and what it said.

    **Never a traceback.** A run record is not a log: the frames and the chain
    of causes go to the log where the run was failed, and what is stored is a
    sentence. ``core.run_error`` makes it storable and cuts it to fit, so
    ending a run never fails over the text of what went wrong.
    """
    said = str(failure).strip()
    named = type(failure).__name__
    return f"{named}: {said}" if said else named
