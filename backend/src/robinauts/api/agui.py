# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The platform's turn events as AG-UI events, and how one crosses SSE.

**The mapping is the whole of what AG-UI is to this build** (``docs/specs/wire.md``).
The application publishes the platform's own events -- ``RunStarted``,
``MessageStarted``, ``TextDelta``, ``ReasoningDelta``, ``MessageCompleted``,
``RunEnded`` -- and this turns each of them into the events a chat client
understands. Nothing below ``api`` knows that AG-UI exists, which is what makes
a second wire, or a client that cannot stream at all, a second mapping rather
than a redesign. The frameworks' AG-UI bridges are not used: every turn goes
through the agent port and the platform's persistence, and the wire is the same
whichever engine produced it.

**The profile is ours.** A stock AG-UI server is handed a ``RunAgentInput``
carrying the history; here the server loads the history from its own store and
the request names the conversation (``robinauts.api.stream_routes``). What that
changes is the *input*, not the events: the package's own event types and its
``EventEncoder`` are what go out, so a client written against AG-UI 1.0 reads
this stream with nothing added.

**Nothing of the platform's own record crosses that is not on this list.**
No ``extras`` -- the format reserves that key on every document it writes and
this build writes none -- and no ``raw_event`` or ``metadata``, which is where a
provider's own payload would otherwise be handed to a browser. A run that ended
badly crosses as a **fixed sentence per state** and never as the stored error,
which is free text made of whatever a provider or a traceback said and is
written for an operator (``robinauts.api.errors``).

**Thinking is bracketed, which is why this is a class.** AG-UI streams
reasoning as a message of its own -- opened, appended to, closed -- while the
platform publishes bare ``ReasoningDelta``s. The brackets are therefore derived
from the sequence: a reasoning delta with no stretch open opens one, and the
first text delta, the completed message or the end of the run closes it. An
answer may hold **several stretches** -- think, say something, think again --
and each is a message of its own. That is state across events, so a mapper
belongs to **one stream** and is built where the stream is; a stream that
begins in the middle is given the state the events before it leave behind
(``seed``), so that it brackets exactly as an unbroken one would.

Each stretch is given an id derived from the answer's and from the position it
opened at (``reasoning_id``) rather than the answer's own: a client that keys
messages by id would otherwise hold one id standing for two messages, and a
second stretch would reopen a message that has been ended. Being derived from
the platform's own numbering, the id is the same one on a re-attach, which no
state carried in this process could promise.

**A span is not opened around them.** AG-UI has ``REASONING_START`` /
``REASONING_END`` for a span that may hold several reasoning messages, and it
is optional: a client reads the reasoning messages on their own, so the span
would be two more events per answer saying what they already say.

**Reasoning is shown and not stored** (``docs/specs/conversations.md``): it is
in the run's events, so a watcher re-attaching in the middle of an answer can
rebuild what it is watching, and in no message, no export and nothing sent back
to a model.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping

from ag_ui.core import (
    BaseEvent,
    ReasoningMessageContentEvent,
    ReasoningMessageEndEvent,
    ReasoningMessageStartEvent,
    RunErrorEvent,
    RunFinishedCancelledOutcome,
    RunFinishedEvent,
    RunStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
)
from ag_ui.encoder import EventEncoder

from robinauts.domain import (
    InvalidValueError,
    MessageCompleted,
    MessageStarted,
    ReasoningDelta,
    RobinautsError,
    Role,
    Run,
    RunEnded,
    RunEvent,
    RunStarted,
    RunState,
    TextDelta,
    describe,
)

SSE_MEDIA_TYPE = "text/event-stream"
"""What the stream is sent as. ``EventEncoder`` says the same; a test asks it."""

REASONING_SUFFIX = ":reasoning:"
"""What tells the id of a stretch of thinking from the id of the answer.

A message id of ours is a uuid, which holds no colon, so no id this platform
issues can collide with one of these. The position of the delta that opened
the stretch follows it (``reasoning_id``).
"""

ENDED_BADLY: Mapping[RunState, str] = {
    RunState.FAILED: "the agent could not finish this answer",
    RunState.INTERRUPTED: "the deployment stopped while this answer was being produced",
}
"""One fixed sentence per way a run ends **in an error**; the ``code`` is the state.

Two of the three faulted states, because the third is not an error on this
wire: a cancellation is ``RUN_FINISHED`` with AG-UI's ``cancelled`` outcome
(``AguiMapper._ended``), which is what the protocol has for a run somebody
stopped.

**Never the run's stored ``error``.** That is whatever a provider or a
traceback said, bounded and kept for an operator (``core.run_error``), and a
body or an event that repeated it would be this project's one place where
something from outside reaches a browser unread. What the person is told is
which of the two happened; what happened is in the record and in the log.
"""

QUIET_CODE = "quiet"
"""The run stored nothing for as long as a whole turn may take."""

GONE_CODE = "gone"
"""The stream is over without the run's own end having been in it."""

INTERNAL_CODE = "internal"
"""Something went wrong here. The event says no more than a 500 body does."""

_ENCODER = EventEncoder()
"""AG-UI's own encoder: one ``data:`` line per event, and the omissions it makes.

Stateless -- it holds the accepted media type and nothing else -- so one is
shared rather than built per stream.
"""

UNMAPPED = "no AG-UI event stands for this kind of turn event"
"""What an event kind nobody mapped raises. A mistake of ours: the platform's
events are a closed set (``domain.TurnEvent``) and every one of them is here,
which ``test_agui`` asks of the union itself."""


SENT_ROLE: Mapping[Role, str] = {
    Role.ASSISTANT: "assistant",
    Role.USER: "user",
}
"""Which of the platform's roles AG-UI has a word for in a **text** message.

Written out rather than taken from the value, because the two sets are not the
same one: the format holds ``tool`` messages -- the results of one batch of
calls (``docs/specs/conversations.md``) -- and AG-UI's ``TEXT_MESSAGE_START``
takes ``developer``, ``system``, ``assistant`` or ``user`` and not ``tool``. A
tool message is not a text message on the wire: its results go out as
``TOOL_CALL_RESULT`` events, each naming its call (``docs/specs/wire.md``),
which is the mapping the wire step writes. Until then a tool message announced
to this mapper is a role with no word, and it stops the stream rather than the
wire carrying a lie.
"""

UNSENDABLE_ROLE = "no AG-UI role stands for this role of ours"
"""What a role with no word in AG-UI raises. See ``SENT_ROLE``."""


def sent_role(role: Role) -> str:
    """That role, as AG-UI names it; ``InvalidValueError`` if it has no name.

    Raised from inside a stream, so it ends it with the generic ``internal``
    event and the whole of it goes to the log
    (``robinauts.api.stream_routes``). Reaching it is a build that started
    producing a kind of message the wire was never taught.
    """
    word = SENT_ROLE.get(role) if isinstance(role, Role) else None
    if word is None:
        # Named where it is one of ours -- a role is a fixed word of the
        # platform's and not something a request carried -- and described
        # where it is not a role at all, which is a bug of another kind.
        said = role.value if isinstance(role, Role) else describe(role)
        raise InvalidValueError(f"{UNSENDABLE_ROLE}: {said}")
    return word


def reasoning_id(message_id: uuid.UUID, position: int) -> str:
    """The id of one stretch of thinking: whose answer, and where it began.

    **Per stretch, not per answer.** A turn may think, say something, and think
    again inside one answer, and each stretch is a reasoning message of its
    own; an id shared between them would open a message a client has already
    been told is over.

    **Derived, so that it survives a re-attach.** A position is the platform's
    and is stable, so a stream seeded with the events up to where it carries on
    from (``AguiMapper.seed``) builds the same id the dropped stream built, and
    the thinking it closes is the one the client has open.
    """
    return f"{message_id}{REASONING_SUFFIX}{position}"


def sse(event: BaseEvent, *, position: int | None = None) -> str:
    """One AG-UI event as a server-sent event, numbered by its position.

    ``id:`` is the event's position in its run, which is what makes re-attaching
    native: a browser's ``EventSource`` remembers the last id it saw and sends
    it back as ``Last-Event-ID``, and that is exactly the ``after`` the stream
    takes (``docs/specs/runs.md``). An event the platform did not number -- one
    this layer made up to say that the stream is over -- carries no ``id:``, so
    a client that re-attaches after it asks from the last **real** position.

    ``event:`` is the AG-UI type, so a client may listen for one kind; the
    ``data:`` line and the blank line after it are the package's encoder's, and
    the JSON in it is the package's own, field names and omissions included.
    """
    numbered = "" if position is None else f"id: {position}\n"
    return f"{numbered}event: {event.type.value}\n{_ENCODER.encode(event)}"


class AguiMapper:
    """One stream's mapping from the platform's turn events to AG-UI's.

    **Its contract is one stream, in order, once.** It is given the events of
    one run as they arrive -- the slice a watcher receives, which may begin
    anywhere -- and answers what to send for each. It carries one piece of
    state, whether a reasoning message is open, so that the brackets AG-UI wants
    around thinking can be derived from the sequence; nothing else about the run
    is remembered, and nothing is read back.

    Two things follow from "the slice may begin anywhere". A message may be
    completed or streamed here without its ``TEXT_MESSAGE_START`` having been in
    this slice, because the client saw it on the connection it lost; and a
    ``MessageCompleted`` is sent as a bare ``TEXT_MESSAGE_END``, with none of
    the message's stored parts, because the client built the message out of the
    deltas. **A client that did not receive every delta of a message reloads the
    conversation** -- which is what opening it does, and what the position in
    ``resume`` is for: a message enters the conversation when it is complete
    (``docs/specs/runs.md``), so the store is what has it and the stream never
    repeats it.
    """

    def __init__(self, *, thread_id: uuid.UUID) -> None:
        self._thread_id = str(thread_id)
        """The conversation, which AG-UI calls the thread.

        Given rather than read off the events: ``RunEnded`` names only its run,
        and a slice that begins after the run started holds no ``RunStarted``
        to take it from. Whoever opens the stream has it -- the turn it began,
        or the run it looked up (``robinauts.api.stream_routes``).
        """
        self._thinking: str | None = None
        """The reasoning message that is open, if one is."""

    def of(self, event: RunEvent) -> tuple[BaseEvent, ...]:
        """What that run event is sent as: none, one, or an ending and one.

        Empty deltas are skipped. AG-UI 1.0.0 accepts one -- its models put no
        minimum length on ``delta`` -- but an empty delta appends nothing to a
        message and opens nothing, so it is a byte on the wire that says
        nothing, and a reasoning delta with nothing in it must not be what opens
        a thinking block.
        """
        inner = event.event
        if isinstance(inner, RunStarted):
            return (RunStartedEvent(thread_id=self._thread_id, run_id=str(inner.run_id)),)
        if isinstance(inner, MessageStarted):
            # A run answers a question and never asks one, so this is always
            # ``assistant`` today. It goes through ``SENT_ROLE`` all the same:
            # the platform's roles and AG-UI's are two sets that happen to
            # overlap, and ``tool`` -- which ``domain.Role`` has reserved -- is
            # in ours and not in theirs.
            #
            # **Resolved before anything is closed.** ``closing`` is a write:
            # it hands back the end of the thinking and forgets it. Building
            # the tuple would run it first, so a role with no word in AG-UI
            # would raise having already taken the one event that says the
            # thinking is over, and the stream's error ending would have
            # nothing left to close with.
            role = sent_role(inner.role)
            return (
                *self.closing(),
                TextMessageStartEvent(message_id=str(inner.message_id), role=role),
            )
        if isinstance(inner, ReasoningDelta):
            return self._thought(inner, event.seq)
        if isinstance(inner, TextDelta):
            if not inner.text:
                return ()
            return (
                *self.closing(),
                TextMessageContentEvent(message_id=str(inner.message_id), delta=inner.text),
            )
        if isinstance(inner, MessageCompleted):
            return (*self.closing(), TextMessageEndEvent(message_id=str(inner.message.id)))
        if isinstance(inner, RunEnded):
            return (*self.closing(), self._ended(inner))
        raise RobinautsError(f"{UNMAPPED}: {type(inner).__name__}")

    def seed(self, events: Iterable[RunEvent]) -> None:
        """Take the state those events leave behind, and send nothing.

        **What a re-attach needs.** The brackets around thinking are derived
        from the sequence, so a stream that begins in the middle of one knows
        nothing about it: the first text delta it sends would not be preceded
        by the ``REASONING_MESSAGE_END`` an unbroken stream would have sent,
        and the client's thinking block would stay open for ever. So the events
        **up to and including** the position being carried on from are run
        through first, and what they leave behind is the state the live stream
        carries on with (``application.Watch.before``).

        It is the mapping itself that is run, with what it answers thrown away,
        rather than a second copy of the rules that could come to disagree with
        it. The ids it derives are the same ones the dropped stream derived,
        because every one of them is built out of the run's own positions.

        Called once, before anything is sent; calling it on a mapper that has
        already sent something would replay events into a state built from
        them.
        """
        for event in events:
            self.of(event)

    def closed(self, run: Run) -> tuple[BaseEvent, ...]:
        """How a run ended, for a stream that did not see the ending itself.

        A watcher that attached at or past the last position is sent nothing
        and stops: everything stored after ``after`` is nothing at all, and the
        run's own ``RunEnded`` was before that (``docs/specs/runs.md``). The
        outcome is not in doubt all the same -- the **record** says it -- so it
        is read and sent, rather than the stream ending with an error over an
        answer that is complete.

        It goes through the same ``_ended`` as a ``RunEnded`` that did arrive,
        over a record built from the run, so there is one mapping from a state
        to what a client is told and the stored error is dropped here exactly
        as it is dropped there.
        """
        return (
            *self.closing(),
            self._ended(RunEnded(run_id=run.id, state=run.state, error=run.error)),
        )

    def closing(self) -> tuple[BaseEvent, ...]:
        """The end of the reasoning message, if one is open; nothing otherwise.

        Called before everything that follows thinking -- the answer's first
        text, its completion, the end of the run -- and by the stream itself
        before an error event, so that a client is never left with a thinking
        block that nothing closed.
        """
        if self._thinking is None:
            return ()
        ending = ReasoningMessageEndEvent(message_id=self._thinking)
        self._thinking = None
        return (ending,)

    def _thought(self, delta: ReasoningDelta, position: int) -> tuple[BaseEvent, ...]:
        """More thinking, opening the reasoning message if this is the first of it.

        ``position`` is where the delta falls in its run, and it is the id of
        the stretch this opens: a turn may think more than once inside one
        answer, and each stretch is a message of its own
        (``reasoning_id``).
        """
        if not delta.text:
            return ()
        opening: tuple[BaseEvent, ...] = ()
        if self._thinking is None:
            self._thinking = reasoning_id(delta.message_id, position)
            opening = (ReasoningMessageStartEvent(message_id=self._thinking),)
        return (
            *opening,
            ReasoningMessageContentEvent(message_id=self._thinking, delta=delta.text),
        )

    def _ended(self, ended: RunEnded) -> BaseEvent:
        """How the run finished, as the one event that says a run is over.

        **A cancellation is not a failure**, and AG-UI 1.0 says so in the
        protocol: ``RUN_FINISHED`` with a ``cancelled`` outcome is "stopped
        before it completed, by whoever was running it, and did not fail". A
        ``RUN_ERROR`` for it would have a stock client show a failure to
        somebody who pressed stop.

        A run that finished is ``RUN_FINISHED`` with no outcome, which reads as
        success. The two that are left are ``RUN_ERROR``, with the state as the
        ``code`` a client branches on and one fixed sentence as the message
        (``ENDED_BADLY``). **Including ``interrupted``**: the platform's
        interrupted is "the process went away with this run in it", which is a
        failure of the deployment's and not AG-UI's ``interrupt`` outcome --
        that one means a run paused waiting for something outside it, and is
        resumed by answering what it asked for.
        """
        if ended.state is RunState.FINISHED:
            return RunFinishedEvent(thread_id=self._thread_id, run_id=str(ended.run_id))
        if ended.state is RunState.CANCELLED:
            return RunFinishedEvent(
                thread_id=self._thread_id,
                run_id=str(ended.run_id),
                outcome=RunFinishedCancelledOutcome(),
            )
        return RunErrorEvent(message=ENDED_BADLY[ended.state], code=ended.state.value)
