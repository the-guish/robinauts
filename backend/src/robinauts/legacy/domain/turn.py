# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What a running turn publishes: the platform's own events, and their envelope.

**What an adapter streams** is the other vocabulary, ``robinauts.legacy.domain.events``:
more text, more thinking, a tool call, its result, and the turn's end -- with
no ids of the platform's, no times and nothing about a run, because an
adapter has none of those (``docs/specs/agents.md``).

**What the application publishes** (``TurnEvent``, in a ``RunEvent`` envelope
with its position) is the same turn with the platform's own facts attached --
which run, which message id, which parent, and the message itself once it is
stored. The application is what turns the first into the second, because it is
what assigns ids, reads the clock and writes the rows.

Keeping them apart is what keeps an adapter from having to invent an id or
claim something is persisted. ``api`` maps the platform's events to AG-UI on
the wire (``docs/specs/wire.md``); they are the platform's, not AG-UI's, so a
second wire -- or a client that cannot stream at all -- is a mapping and not a
redesign.

**A stream is a view of a run, not the run** (``docs/specs/runs.md``). What
keeps a dropped request from losing anything is not the stream: it is that the
run goes on and that its events are kept, each under a position. A message
enters the conversation when it is **complete**; a message still being
produced is in the events and nowhere else. So a watcher that attaches in the
middle loads the messages that are finished and replays the events from the
position it last saw, and rebuilds the half-written one without seeing
anything twice.

``RunEvent`` is that envelope: the event, and where in its run it falls. The
application numbers them, from 1, one after another with no gaps.

The order of a run's events is fixed, and
``robinauts.legacy.core.runs.check_event_order`` is the one statement of it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from robinauts.legacy.domain.conversation import MAX_PART_CHARS, Message, Role, check_supported_role
from robinauts.legacy.domain.errors import InvalidValueError
from robinauts.legacy.domain.run import (
    ENDED_RUN_STATES,
    FAULTED_RUN_STATES,
    MAX_RUN_ERROR_CHARS,
    RunState,
)
from robinauts.legacy.domain.tools import checked_call_id, checked_tool_name
from robinauts.legacy.domain.values import checked_text, checked_uuid, describe


@dataclass(frozen=True, slots=True)
class RunStarted:
    """A run has begun. The first event of every stream."""

    run_id: uuid.UUID
    conversation_id: uuid.UUID

    def __post_init__(self) -> None:
        checked_uuid(self.run_id, "a run's id")
        checked_uuid(self.conversation_id, "a conversation's id")


@dataclass(frozen=True, slots=True)
class MessageStarted:
    """A message is being produced, under this id; its parts follow as deltas.

    It says what the message will be before any of it exists -- its role and
    where it hangs -- so that a watcher can put it in the tree it is already
    showing rather than wait for the whole of it.
    """

    run_id: uuid.UUID
    message_id: uuid.UUID
    parent_id: uuid.UUID
    """What it answers. A message a run produces is never a root."""
    role: Role = Role.ASSISTANT

    def __post_init__(self) -> None:
        checked_uuid(self.run_id, "a run's id")
        checked_uuid(self.message_id, "a message's id")
        checked_uuid(self.parent_id, "a message's parent id")
        if self.parent_id == self.message_id:
            raise InvalidValueError("a message cannot be its own parent")
        if not isinstance(self.role, Role):
            raise InvalidValueError(f"a message's role is a Role, not {describe(self.role)}")
        check_supported_role(self.role)
        # A run answers a question; it never announces one.
        if self.role is Role.USER:
            raise InvalidValueError("a run produces answers, not questions")


@dataclass(frozen=True, slots=True)
class TextPiece:
    """More of the text of the message being produced.

    **Storable text**, unlike what an engine yields: this is written into the
    run's events and sent over the wire, so it has to be encodable. A provider
    splits its answer where it likes, and ``robinauts.legacy.domain.publishable`` is
    what turns its fragments into these -- holding back a half of a character
    until its other half arrives, rather than publishing a piece that nothing
    can carry.
    """

    run_id: uuid.UUID
    message_id: uuid.UUID
    text: str

    def __post_init__(self) -> None:
        checked_uuid(self.run_id, "a run's id")
        checked_uuid(self.message_id, "a message's id")
        checked_text(self.text, "a delta's text", MAX_PART_CHARS)


@dataclass(frozen=True, slots=True)
class ReasoningPiece:
    """More of the thinking of the message being produced.

    Storable text, like ``TextPiece``, and for the same reason. Shown as it
    arrives, collapsed under the answer, and **not stored as content**: this
    version keeps no reasoning (``docs/working-notes/poc-scope.md``, "Out").
    An engine that yields ``AnswerReasoningDelta`` has it published as this,
    and a ``ReasoningPart`` among the parts of a completed answer is dropped
    by the application rather than refused -- an engine is not asked to know
    what this version keeps.
    """

    run_id: uuid.UUID
    message_id: uuid.UUID
    text: str

    def __post_init__(self) -> None:
        checked_uuid(self.run_id, "a run's id")
        checked_uuid(self.message_id, "a message's id")
        checked_text(self.text, "a delta's text", MAX_PART_CHARS)


@dataclass(frozen=True, slots=True)
class MessageCompleted:
    """A message is complete and persisted; this is it, as it was stored."""

    run_id: uuid.UUID
    message: Message

    def __post_init__(self) -> None:
        checked_uuid(self.run_id, "a run's id")
        if not isinstance(self.message, Message):
            raise InvalidValueError(
                f"a completed message is a Message, not {describe(self.message)}"
            )


@dataclass(frozen=True, slots=True)
class RunEnded:
    """The run is over, in ``state``. The last event of every stream.

    One event carrying the run's own state rather than four events that would
    have to be kept in step with it: a watcher switches on the state it will
    read from the run record anyway, and a state added to a run cannot be
    forgotten here.
    """

    run_id: uuid.UUID
    state: RunState
    error: str | None = None
    """What went wrong, when it went wrong. Required for a failure."""

    def __post_init__(self) -> None:
        checked_uuid(self.run_id, "a run's id")
        if not isinstance(self.state, RunState):
            raise InvalidValueError(f"a run's state is a RunState, not {describe(self.state)}")
        if self.state not in ENDED_RUN_STATES:
            raise InvalidValueError(f"a run in {self.state.value} has not ended")
        if self.state is RunState.FAILED and not self.error:
            raise InvalidValueError("a failed run says what went wrong")
        if self.error is not None:
            checked_text(self.error, "a run's error", MAX_RUN_ERROR_CHARS)
            if self.state not in FAULTED_RUN_STATES:
                raise InvalidValueError(f"a run that ended {self.state.value} has no error")


@dataclass(frozen=True, slots=True)
class CallStarted:
    """The answer being produced is making a tool call: its id, and the tool's full name.

    Inside an assistant message, after its announcement and before its
    completion, as the engine announced it (``ToolCallStarted``) and with the
    platform's facts attached. The id is the vendor's, carried as data, and
    is what the call's arguments, its completion and its result are matched
    by -- on the wire, in the events and in the conversation
    (``docs/specs/wire.md``).
    """

    run_id: uuid.UUID
    message_id: uuid.UUID
    call_id: str
    name: str

    def __post_init__(self) -> None:
        checked_uuid(self.run_id, "a run's id")
        checked_uuid(self.message_id, "a message's id")
        checked_call_id(self.call_id, "a tool call's id")
        checked_tool_name(self.name, "a tool's name")


@dataclass(frozen=True, slots=True)
class ArgumentsPiece:
    """More of the arguments of the call being made, as the model writes them.

    Storable text, like ``TextPiece``, and for the same reason; JSON once the
    pieces are joined, which the completed message's part is the parsed form
    of (``docs/specs/runs.md``, "what was published is what was stored").
    """

    run_id: uuid.UUID
    message_id: uuid.UUID
    call_id: str
    text: str

    def __post_init__(self) -> None:
        checked_uuid(self.run_id, "a run's id")
        checked_uuid(self.message_id, "a message's id")
        checked_call_id(self.call_id, "a tool call's id")
        checked_text(self.text, "a delta's text", MAX_PART_CHARS)


@dataclass(frozen=True, slots=True)
class CallCompleted:
    """The call being made is whole; the part it became is in the message that completes."""

    run_id: uuid.UUID
    message_id: uuid.UUID
    call_id: str

    def __post_init__(self) -> None:
        checked_uuid(self.run_id, "a run's id")
        checked_uuid(self.message_id, "a message's id")
        checked_call_id(self.call_id, "a tool call's id")


@dataclass(frozen=True, slots=True)
class ResultLanded:
    """One result of the tool message being produced, as it landed.

    Inside a **tool** message, announced under the answer that made the calls
    and completed when the last result is in; each names the call it answers,
    and the completed message holds exactly these, as ``ToolResultPart``s
    (``docs/specs/runs.md``, "Tools"). Storable text, bounded as a part is.
    """

    run_id: uuid.UUID
    message_id: uuid.UUID
    call_id: str
    text: str
    is_error: bool = False

    def __post_init__(self) -> None:
        checked_uuid(self.run_id, "a run's id")
        checked_uuid(self.message_id, "a message's id")
        checked_call_id(self.call_id, "a tool call's id")
        checked_text(self.text, "a result's text", MAX_PART_CHARS)
        if not isinstance(self.is_error, bool):
            raise InvalidValueError(
                f"whether a result is an error is yes or no, not {describe(self.is_error)}"
            )


TurnEvent = (
    RunStarted
    | MessageStarted
    | TextPiece
    | ReasoningPiece
    | CallStarted
    | ArgumentsPiece
    | CallCompleted
    | ResultLanded
    | MessageCompleted
    | RunEnded
)
"""Everything the application publishes for a running turn, as a closed set."""


FIRST_POSITION = 1
"""Where a run's events are numbered from."""

MAX_POSITION = 2**53 - 1
"""How far they count.

Past this a whole number stops being one everywhere it has to travel: JSON
has one number type and a browser reads it as a double. No run comes near it;
a position that did would have got there by a bug, and a bound is how that is
found rather than written out as something else.
"""


@dataclass(frozen=True, slots=True)
class RunEvent:
    """One event of a run, and where in the run it falls.

    ``seq`` starts at ``FIRST_POSITION`` and counts up by one, with no gaps,
    for the life of the run. It is what a watcher re-attaches by: "everything
    after 17, then the rest, live". The application assigns it -- an engine
    yields bare events and never sees a position -- so that one run has one
    numbering whichever engine produced it.
    """

    run_id: uuid.UUID
    seq: int
    event: TurnEvent

    def __post_init__(self) -> None:
        checked_uuid(self.run_id, "a run's id")
        if isinstance(self.seq, bool) or not isinstance(self.seq, int):
            raise InvalidValueError(
                f"an event's position is a whole number, not {describe(self.seq)}"
            )
        if not FIRST_POSITION <= self.seq <= MAX_POSITION:
            raise InvalidValueError(
                f"an event's position is between {FIRST_POSITION} and {MAX_POSITION},"
                f" not {self.seq}"
            )
        if not isinstance(self.event, TurnEvent):
            raise InvalidValueError(f"a run event carries a turn event, not {describe(self.event)}")
        if self.event.run_id != self.run_id:
            raise InvalidValueError("a run event and the event it carries name one run")
