# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Where a run may go from where it is, and whether a new one may start.

The run lifecycle belongs to the application (``docs/specs/runs.md``); the
rules it follows are here, as a table and three functions over it, so that
every way a run ends -- a normal finish, a cancellation, a process that went
away, a failure -- goes through one check. ``now`` is passed in: nothing here
reads a clock.

The table:

- ``running`` may suspend on a tool call, finish, fail, be cancelled by its
  author, or be found ``interrupted`` after the process that owned it went
  away;
- ``waiting`` holds no process, so nothing can interrupt it: a tool result
  resumes it to ``running``, its author cancels it, or it fails;
- the four ended states are ended. A retry is a **new** run from the
  conversation as it stands, never a run going backwards.

**Ending a run never fails.** What went wrong is whatever a provider, a
framework or a traceback said, at whatever length and in whatever characters;
``run_error`` makes it storable and short enough before the record is built,
so a run cannot be left ``running`` for ever because its failure would not fit.

**Reading rows is not reading a request.** ``active_run_stored`` and
``check_may_start_run_stored`` are what the **application** calls for runs a
store returned: rows of ours that contradict each other -- two runs going at
once in one conversation -- are a fault of the deployment, raised as
``StoredDataError``. A store builds the ``Run`` records themselves, inside
``robinauts.domain.reading_stored``; it never calls anything here, because it
may not import ``core`` at all (``docs/layout.md``). The plain forms are for
runs assembled in a request or a test.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from robinauts.domain import (
    ENDED_RUN_STATES,
    FAULTED_RUN_STATES,
    FIRST_POSITION,
    MAX_RUN_ERROR_CHARS,
    AnswerCompleted,
    AnswerReasoningDelta,
    AnswerStarted,
    AnswerTextDelta,
    EngineEvent,
    IllegalTransitionError,
    InvalidValueError,
    Message,
    MessageCompleted,
    MessageStarted,
    ReasoningDelta,
    Role,
    Run,
    RunAlreadyActiveError,
    RunEnded,
    RunEvent,
    RunStarted,
    RunState,
    TextDelta,
    TextPart,
    ToolCallArgumentsDelta,
    ToolCallCompleted,
    ToolCallPart,
    ToolCallStarted,
    TurnEvent,
    WaitingOnTools,
    checked_uuid,
    clean_text,
    describe,
    reading_stored,
)

RUN_TRANSITIONS: Mapping[RunState, frozenset[RunState]] = {
    RunState.RUNNING: frozenset(
        {
            RunState.WAITING,
            RunState.FINISHED,
            RunState.FAILED,
            RunState.CANCELLED,
            RunState.INTERRUPTED,
        }
    ),
    RunState.WAITING: frozenset({RunState.RUNNING, RunState.FAILED, RunState.CANCELLED}),
    RunState.FINISHED: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.CANCELLED: frozenset(),
    RunState.INTERRUPTED: frozenset(),
}
"""Every state, and the states it may be followed by. The whole rule."""


def may_transition(current: RunState, target: RunState) -> bool:
    """Whether a run in ``current`` may go to ``target``.

    A state is never its own successor: a second "finish it" is a bug in the
    caller, not a no-op to be swallowed.
    """
    if not isinstance(current, RunState) or not isinstance(target, RunState):
        raise InvalidValueError(
            f"a run's states are RunStates, not {describe(current)} and {describe(target)}"
        )
    return target in RUN_TRANSITIONS[current]


def check_transition(run: Run, target: RunState) -> None:
    """``IllegalTransitionError`` unless ``run`` may go to ``target``."""
    if not may_transition(run.state, target):
        raise IllegalTransitionError(
            f"run {run.id} is {run.state.value} and cannot become {target.value}"
        )


def transition(run: Run, target: RunState, *, now: datetime, error: str | None = None) -> Run:
    """``run`` in ``target``, with the times that state asks for.

    The end is stamped when it ends, and the start when a process first takes
    it up; a run resumed from ``waiting`` keeps the start it already had, so
    how long it ran is how long it ran.

    **The stamps are clamped to the one before them.** A record does not
    refuse to exist because a wall clock stepped backwards (``Run``), and a
    run whose end came before its start would be nonsense in every listing, so
    what is written here is never earlier than what it follows. A run that was
    executing and never recorded a start is given one as it ends, for the same
    reason: an ended run with no beginning is a row nothing can measure.

    **It never fails over the error text.** However long, however spelt, what
    went wrong goes through ``run_error`` first: a failure that could not be
    written down would leave the run ``running`` for ever, which is the one
    outcome worse than a truncated message.
    """
    check_transition(run, target)
    if error is not None and target not in FAULTED_RUN_STATES:
        raise InvalidValueError(
            f"a run that ends {target.value} records no error; it was given one"
        )
    started_at = run.started_at
    ending = target in ENDED_RUN_STATES
    if started_at is None and (
        target is RunState.RUNNING or (ending and run.state is RunState.RUNNING)
    ):
        started_at = max(now, run.created_at)
    finished_at = max(now, started_at or run.created_at) if ending else None
    kept = None
    if target in FAULTED_RUN_STATES and (error is not None or target is RunState.FAILED):
        kept = run_error(error)
    return replace(
        run,
        state=target,
        started_at=started_at,
        finished_at=finished_at,
        error=kept,
    )


UNSAID_ERROR = "the run failed and said nothing about why"
"""What a failure with no message of its own records, so that it records one."""

TRUNCATED = "\u2026 (cut; the whole of it is in the log)"
"""What a cut error ends with, so that a truncation reads as one."""


def run_error(error: str | None) -> str:
    """``error`` as a run may record it: storable, bounded, and never nothing.

    Whoever builds a ``RunEnded`` uses this too, so that the event and the
    record say the same thing. What is cut is not lost: the whole of it goes
    to the log where the run was failed.
    """
    if error is None or not isinstance(error, str) or not error.strip():
        return UNSAID_ERROR
    text = clean_text(error)
    if not text.strip():
        return UNSAID_ERROR
    if len(text) <= MAX_RUN_ERROR_CHARS:
        return text
    return text[: MAX_RUN_ERROR_CHARS - len(TRUNCATED)] + TRUNCATED


def active_run(runs: Iterable[Run]) -> Run | None:
    """The one run of a conversation that is still going, if there is one.

    Two at once is not a state to choose between: it means two answers are
    being written into one conversation, so it is reported rather than
    resolved.
    """
    active = [run for run in runs if run.is_active]
    if len(active) > 1:
        raise InvalidValueError(
            f"a conversation has at most one active run, not {len(active)}:"
            f" {', '.join(str(run.id) for run in active)}"
        )
    return active[0] if active else None


def active_run_stored(runs: Iterable[Run]) -> Run | None:
    """``active_run`` over rows of our own store; a contradiction is ours."""
    with reading_stored("a stored conversation has more than one run going"):
        return active_run(runs)


def may_start_run(runs: Iterable[Run]) -> bool:
    """Whether a new run may start: only if none of ``runs`` is still going."""
    return active_run(runs) is None


def check_may_start_run(runs: Iterable[Run]) -> None:
    """``RunAlreadyActiveError`` if this conversation is already answering.

    A conversation has at most one active run (``docs/specs/runs.md``): while
    one is going, a new message is refused and the person may cancel it.
    """
    _refuse_if_active(active_run(runs))


def check_may_start_run_stored(runs: Iterable[Run]) -> None:
    """``check_may_start_run`` over rows of our own store."""
    _refuse_if_active(active_run_stored(runs))


def _refuse_if_active(running: Run | None) -> None:
    if running is not None:
        raise RunAlreadyActiveError(
            f"run {running.id} is {running.state.value} in conversation"
            f" {running.conversation_id}; cancel it or wait for it"
        )


# --- what an engine yields ---------------------------------------------------

_ENGINE_DELTAS = (AnswerTextDelta, AnswerReasoningDelta)


def check_engine_events(events: Iterable[EngineEvent], *, cut_short: bool = False) -> None:
    """The order an engine's events come in; ``InvalidValueError`` if they do not.

    What the shared contract suite holds both engines to
    (``docs/specs/agents.md``):

    - an answer is announced by ``AnswerStarted``, then its deltas, then
      ``AnswerCompleted``. One answer at a time, and a turn may hold several,
      one after another;
    - **streaming is optional, and what is streamed is what is kept.** An
      answer that yielded no text delta may complete with any text: not every
      provider, and not every path through one, streams. An answer that
      yielded **any** text delta must complete with exactly what it streamed,
      joined and made storable -- the platform's promise is that what a person
      watched arrive is what was stored, so an engine whose framework rewrites
      the final message builds its parts from what it streamed, or does not
      stream;
    - reasoning is held to nothing of the sort. An engine may yield
      ``AnswerReasoningDelta`` and complete with no reasoning at all: this
      version shows reasoning as it arrives and does not keep it;
    - **a tool call is inside an answer, one at a time**: announced by
      ``ToolCallStarted`` with its id and name, its arguments streamed by
      ``ToolCallArgumentsDelta`` for that call, closed by ``ToolCallCompleted``
      with the platform's part for it -- the same id and name, and arguments
      the streamed JSON parses to, when any was streamed. The answer then
      completes holding exactly the calls it announced, in that order, and no
      call twice;
    - **an answer that asked for tools ends the turn waiting**: it is followed
      by ``WaitingOnTools`` and by nothing else, since the engine never runs a
      tool -- the application does, and starts the next turn from the stored
      history (``docs/specs/runs.md``, "Tools"). An answer that asked for none
      is followed by no such event;
    - nothing about a run: an engine has no ids of the platform's, no clock and
      no rows, and the events that name those belong to the application.

    **A turn produces at least one answer.** A run that ends with none is a
    run that failed -- "the engine produced no answer" -- and never a finished
    one (``docs/specs/runs.md``), so a stream with nothing in it is refused
    here rather than turned into a conversation with a turn missing from it.

    For a turn that ran to its end. **A turn cut short is not this**: an engine
    reports a failure by raising, and a cancellation closes it where it stands
    (``docs/specs/runs.md``), so what it yielded ends wherever it ended.
    ``cut_short=True`` is for checking those: everything above holds, an
    answer left announced and never completed is allowed at the end -- a call
    left open inside it too -- and so is a stream that never began one, or
    one that completed an answer with calls and never said it was waiting.
    """
    open_answer = False
    answers = 0
    streamed: list[str] = []
    calls: list[ToolCallPart] = []
    open_call: ToolCallStarted | None = None
    arguments: list[str] = []
    waiting: bool | None = None
    """Whether the last completed answer asked for tools; ``None`` before one has."""
    over = False
    for event in events:
        if isinstance(event, TurnEvent):
            raise InvalidValueError(
                "an engine yields what the model said; a run's own events are the" " application's"
            )
        if not isinstance(event, EngineEvent):
            raise InvalidValueError(f"an engine yields engine events, not {describe(event)}")
        if over:
            raise InvalidValueError("nothing follows a turn that ended waiting on tools")
        if isinstance(event, AnswerStarted):
            if open_answer:
                raise InvalidValueError("an engine produces one answer at a time")
            if waiting:
                raise InvalidValueError("an answer that asked for tools ends the turn waiting")
            open_answer, streamed, calls, answers = True, [], [], answers + 1
        elif isinstance(event, _ENGINE_DELTAS):
            if not open_answer:
                raise InvalidValueError("a delta belongs to an answer being produced")
            if isinstance(event, AnswerTextDelta):
                streamed.append(event.text)
        elif isinstance(event, ToolCallStarted):
            if not open_answer:
                raise InvalidValueError("a tool call belongs to an answer being produced")
            if open_call is not None:
                raise InvalidValueError("an engine announces one tool call at a time")
            if any(call.call_id == event.call_id for call in calls):
                raise InvalidValueError("an answer names each tool call once")
            open_call, arguments = event, []
        elif isinstance(event, ToolCallArgumentsDelta):
            if open_call is None or event.call_id != open_call.call_id:
                raise InvalidValueError("arguments belong to the tool call being made")
            arguments.append(event.text)
        elif isinstance(event, ToolCallCompleted):
            if open_call is None:
                raise InvalidValueError("a tool call is completed once, after it was announced")
            _check_call(event.call, open_call, arguments)
            calls.append(event.call)
            open_call = None
        elif isinstance(event, AnswerCompleted):
            if not open_answer:
                raise InvalidValueError("an answer is completed once, after it was announced")
            if open_call is not None:
                raise InvalidValueError("a tool call that was announced is completed")
            said = "".join(part.text for part in event.parts if isinstance(part, TextPart))
            # An answer that streamed nothing -- or nothing but empty deltas,
            # which is the same thing -- may complete with any text.
            joined = clean_text("".join(streamed))
            if joined and joined != said:
                raise InvalidValueError("an answer's text deltas are the text it completed with")
            if list(part for part in event.parts if isinstance(part, ToolCallPart)) != calls:
                raise InvalidValueError("an answer completes with exactly the calls it announced")
            open_answer, waiting = False, bool(calls)
        elif isinstance(event, WaitingOnTools):
            if open_answer or not waiting:
                raise InvalidValueError(
                    "a turn waits on the tools the answer it completed asked for"
                )
            over = True
    if not cut_short:
        if open_answer:
            raise InvalidValueError("an answer that was announced is completed")
        if not answers:
            raise InvalidValueError("a turn produces an answer; one that produces none failed")
        if waiting and not over:
            raise InvalidValueError("an answer that asked for tools ends the turn waiting")


def _check_call(call: ToolCallPart, started: ToolCallStarted, arguments: Sequence[str]) -> None:
    """The call that completes is the call that was announced, with what was streamed.

    The arguments streamed are JSON text and the part holds them as data, so
    the check is that the text, joined, parses to exactly that mapping -- as
    JSON, where ``1``, ``1.0`` and ``true`` are three values, not as Python,
    where they are one; a call that streamed nothing -- not every provider
    does -- may complete with any arguments, as an answer that streamed no
    text may. The deltas are bounded one by one and not in number, so text
    nested past what the parser can follow is refused like any other text
    that is not the call's JSON, rather than escaping as a ``RecursionError``.
    """
    if call.call_id != started.call_id or call.name != started.name:
        raise InvalidValueError("a tool call is completed as it was announced")
    joined = clean_text("".join(arguments))
    if not joined.strip():
        return
    try:
        parsed = json.loads(joined)
    except (ValueError, RecursionError):
        raise InvalidValueError("a tool call's streamed arguments are JSON") from None
    if json.dumps(parsed, sort_keys=True) != json.dumps(call.arguments, sort_keys=True):
        raise InvalidValueError(
            "a tool call's streamed arguments are the arguments it completed with"
        )


@dataclass(frozen=True, slots=True)
class ResumePoint:
    """Where a watcher attaches to a run in flight, and what to expect there.

    ``after`` is the position to carry on from; ``follows`` is the id the next
    message announced after it will hang under. A caller that has one of these
    has everything ``check_event_order`` needs for a slice, which is the point
    of handing back both.
    """

    after: int
    follows: uuid.UUID | None


def resume_point(events: Sequence[RunEvent], *, answering: uuid.UUID) -> ResumePoint:
    """Where a watcher opening this conversation attaches.

    Opening a conversation that has a run in flight loads the messages that
    are **stored** -- every one that is complete -- and then watches the rest
    arrive. Where it must start watching is therefore after the last message
    the run completed: the events before that are messages it already has, and
    the events after it are the one still being produced, from its
    ``MessageStarted``. So nothing is shown twice and nothing is missed
    (``docs/specs/runs.md``).

    ``follows`` is that last completed message, since that is what the next
    announcement will hang under; ``answering`` -- the message the run answers
    -- is what it is when nothing has completed yet, and the caller passes it
    because only it knows. It is required: a point that did not know what the
    next message follows would hand ``check_event_order`` a ``None`` and
    quietly turn that check off.

    If the run has completed nothing, ``after`` is the position of
    ``RunStarted``, which is ``FIRST_POSITION``; ``0`` for a run with no
    events at all, which is a run that has not begun.

    Pure, and the definition a store's query has to match: "the position of
    this run's last ``MessageCompleted`` event, or of its ``RunStarted``".
    """
    after = 0
    follows = checked_uuid(answering, "the message a run answers")
    for event in events:
        if not isinstance(event, RunEvent):
            raise InvalidValueError(f"a run's events are numbered, not {describe(event)}")
        if isinstance(event.event, RunStarted) and not after:
            after = event.seq
        elif isinstance(event.event, MessageCompleted):
            after, follows = event.seq, event.event.message.id
    return ResumePoint(after=after, follows=follows)


# --- the order of a run's events --------------------------------------------

_DELTAS = (TextDelta, ReasoningDelta)


def check_event_order(
    events: Iterable[RunEvent | TurnEvent],
    *,
    run_id: uuid.UUID,
    conversation_id: uuid.UUID,
    follows: uuid.UUID,
    after: int = 0,
    open_message: uuid.UUID | None = None,
    ended: bool = True,
) -> None:
    """The order a run's events come in; ``InvalidValueError`` if they do not.

    The guarantee, which the wire and the application are held to
    (``docs/specs/runs.md``):

    - one ``RunStarted`` first and one ``RunEnded`` last, with nothing after
      it. A run that ended ``finished`` completed at least one message and
      left none half-written; one that failed, was cancelled or was
      interrupted may leave a message announced and never completed, which is
      what a cancellation in the middle of an answer looks like;
    - **one message at a time**: a message is announced by ``MessageStarted``,
      then its deltas, then ``MessageCompleted``, and only then may another be
      announced. A turn is a chain and never forks;
    - each message hangs under the one before it: the first under the message
      the run answers (``answering``), each later one under the message
      completed before it;
    - a completed message is the one that was announced, with the role and the
      parent it was announced with; it belongs to ``conversation_id``, it
      records the run that produced it, and it is never a question -- a run
      produces answers;
    - **what was published is what was stored**: if any text was published
      for a message, the deltas joined are the text of the message that
      completed it. A watcher that saw an answer arrive has the answer that
      is in the conversation, character for character;
    - every event names ``run_id``, and, where they are numbered
      (``RunEvent``), the numbers continue from ``after`` by one with no gaps.

    ``run_id``, ``conversation_id`` and ``follows`` are **required**: they are
    three of the guarantees above, and a caller that left one out would be
    quietly checking less than it thinks. ``follows`` is the message the next
    announcement must hang under -- for a whole run, the message the run
    answers; for a slice, what ``resume_point`` said.

    ``after`` is what a **watcher re-attaching** passes: the position it last
    saw. The numbers must continue from it, ``RunStarted`` cannot be in such a
    slice, and the slice may begin in the middle of a message -- deltas and a
    completion for one message announced before the cut are what it begins
    with. ``open_message`` says which message that is, for a caller that knows;
    ``answering`` is optional there, because the message the run answers may
    have been announced before the cut.
    """
    checked_uuid(run_id, "a run's id")
    checked_uuid(conversation_id, "a conversation's id")
    checked_uuid(follows, "the message a new message follows")
    if open_message is not None:
        checked_uuid(open_message, "the message a slice begins inside")
    if isinstance(after, bool) or not isinstance(after, int) or after < 0:
        raise InvalidValueError(
            f"a position to carry on from is a whole number, not {describe(after)}"
        )
    a_slice = after > 0
    if not a_slice and open_message is not None:
        raise InvalidValueError("only a slice of a stream begins inside a message")
    bare = _bare(list(events), after)
    if any(event.run_id != run_id for event in bare):
        raise InvalidValueError("every event of a stream belongs to the run it is of")

    started = over_count = completed = 0
    announced: dict[uuid.UUID, MessageStarted] = {}
    open_id = open_message
    # A slice may begin inside a message the caller did not name; the first
    # event about it, and only the first, says which one it is.
    may_adopt = a_slice and open_message is None
    published: list[str] = []
    # Whether this slice began in the middle of a message. Then what the
    # first message announced in it follows was completed before the cut, and
    # there is nothing here to compare it with.
    began_inside = open_message is not None
    last_completed: uuid.UUID | None = None
    over: RunEnded | None = None

    for at, event in enumerate(bare):
        if over_count:
            raise InvalidValueError("a run's stream ends with the event that ends the run")
        if isinstance(event, RunStarted):
            if a_slice or at != 0:
                raise InvalidValueError("a run starts once, before anything else")
            if event.conversation_id != conversation_id:
                raise InvalidValueError("a run belongs to the conversation it answers in")
            started += 1
        elif isinstance(event, RunEnded):
            over_count += 1
            over = event
        elif isinstance(event, MessageStarted):
            if open_id is not None:
                raise InvalidValueError("a run produces one message at a time")
            if event.message_id in announced:
                raise InvalidValueError("a message is announced once")
            _check_chain(event, last_completed, follows, began_inside)
            announced[event.message_id] = event
            open_id, published = event.message_id, []
        elif isinstance(event, _DELTAS):
            if open_id is None:
                if not (may_adopt and not announced and last_completed is None):
                    raise InvalidValueError("a delta belongs to a message being produced")
                open_id, may_adopt, began_inside = event.message_id, False, True
            if event.message_id != open_id:
                raise InvalidValueError("a delta belongs to a message being produced")
            if isinstance(event, TextDelta):
                published.append(event.text)
        elif isinstance(event, MessageCompleted):
            message = event.message
            if open_id is None:
                if not (may_adopt and not announced and last_completed is None):
                    raise InvalidValueError("a message is completed once, after it was announced")
                open_id, may_adopt, began_inside = message.id, False, True
            if message.id != open_id:
                raise InvalidValueError("a message is completed once, after it was announced")
            _check_completed(message, announced.get(message.id), run_id, conversation_id)
            # A slice that began inside a message did not see its first
            # deltas, so there is nothing to compare for that one.
            # On the joined text, as the engine check is: a delta that
            # carried nothing is nothing published.
            watched = "".join(published) if message.id in announced else ""
            if watched and watched != message.text:
                raise InvalidValueError(
                    "what was published for a message is the message that was stored"
                )
            last_completed, open_id, published = message.id, None, []
            completed += 1

    if not a_slice and (started != 1 or (over_count != 1 and ended)) and (bare or ended):
        raise InvalidValueError("a run's stream starts with its run and ends with it")
    if over_count and not ended:
        raise InvalidValueError("a run that is still going has not ended")
    if over is not None and over.state is RunState.FINISHED:
        # It finished: there is nothing half-written and there is an answer.
        if open_id is not None:
            raise InvalidValueError("a run that finished left no message half-written")
        if not a_slice and not completed:
            raise InvalidValueError("a run that finished produced a message")


def _check_chain(
    event: MessageStarted,
    last_completed: uuid.UUID | None,
    follows: uuid.UUID,
    began_inside: bool,
) -> None:
    """A turn is a chain: each message hangs under the one before it.

    The first message of a stream hangs under what the stream said it
    follows. In a slice that began in the middle of a message there is
    nothing here to compare the next one with -- what it follows is completed
    inside this slice -- and then nothing is checked until it has been.
    """
    expected = last_completed if last_completed is not None else (None if began_inside else follows)
    if expected is not None and event.parent_id != expected:
        raise InvalidValueError("a run's messages follow one another in a chain")


def _check_completed(
    message: Message,
    start: MessageStarted | None,
    run_id: uuid.UUID | None,
    conversation_id: uuid.UUID,
) -> None:
    """The message that arrives is the message that was announced."""
    if message.role is Role.USER:
        raise InvalidValueError("a run produces answers, not questions")
    if message.conversation_id != conversation_id:
        raise InvalidValueError("a run's messages belong to its own conversation")
    if message.role is Role.ASSISTANT and (
        message.provenance is None or message.provenance.run_id != run_id
    ):
        raise InvalidValueError("a message a run produced records that run")
    if start is not None and (
        message.parent_id != start.parent_id or message.role is not start.role
    ):
        raise InvalidValueError("a message is completed as it was announced")


def _bare(events: Sequence[RunEvent | TurnEvent], after: int) -> list[TurnEvent]:
    """The events themselves, with their numbering checked in one pass.

    All numbered or none of them: a stream where some carry a position and
    some do not cannot be placed at all. Linear -- a check per event that
    looked at every other event would make a long answer quadratic, and an
    answer's events are one per token.
    """
    numbered = [isinstance(event, RunEvent) for event in events]
    if any(numbered) and not all(numbered):
        raise InvalidValueError("a run's events are all numbered, or none of them is")
    if events and after and not all(numbered):
        raise InvalidValueError("a slice of a run's stream is numbered, or it cannot be placed")
    bare: list[TurnEvent] = []
    for at, event in enumerate(events):
        if isinstance(event, RunEvent):
            if event.seq != after + FIRST_POSITION + at:
                raise InvalidValueError(
                    f"a run's events are numbered from {after + FIRST_POSITION} with no gaps"
                )
            bare.append(event.event)
        elif isinstance(event, TurnEvent):
            bare.append(event)
        else:
            raise InvalidValueError(f"a run's stream carries turn events, not {describe(event)}")
    return bare
