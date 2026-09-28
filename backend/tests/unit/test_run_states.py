# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Where a run may go from where it is, and whether a new one may start."""

import itertools
import random
import time
import uuid
from collections.abc import Iterable
from dataclasses import replace

import pytest

from conversations import (
    CONVERSATION,
    OTHER_CONVERSATION,
    RUN,
    answer,
    at,
    ended,
    provenance,
    question,
    run,
)
from robinauts.core import (
    RUN_TRANSITIONS,
    TRUNCATED,
    UNSAID_ERROR,
    ResumePoint,
    active_run,
    active_run_stored,
    check_engine_events,
    check_event_order,
    check_may_start_run,
    check_may_start_run_stored,
    check_transition,
    may_start_run,
    may_transition,
    resume_point,
    run_error,
    transition,
)
from robinauts.domain import (
    ACTIVE_RUN_STATES,
    ENDED_RUN_STATES,
    FIRST_POSITION,
    MAX_RUN_ERROR_CHARS,
    AnswerCompleted,
    AnswerReasoningDelta,
    AnswerStarted,
    AnswerTextDelta,
    IllegalTransitionError,
    InvalidValueError,
    Message,
    MessageCompleted,
    MessageStarted,
    ReasoningDelta,
    ReasoningPart,
    RunAlreadyActiveError,
    RunEnded,
    RunEvent,
    RunStarted,
    RunState,
    StoredDataError,
    TextDelta,
    TextPart,
    ToolCallArgumentsDelta,
    ToolCallCompleted,
    ToolCallPart,
    ToolCallStarted,
    WaitingOnTools,
)

LEGAL = {
    (RunState.RUNNING, RunState.WAITING),
    (RunState.RUNNING, RunState.FINISHED),
    (RunState.RUNNING, RunState.FAILED),
    (RunState.RUNNING, RunState.CANCELLED),
    (RunState.RUNNING, RunState.INTERRUPTED),
    (RunState.WAITING, RunState.RUNNING),
    (RunState.WAITING, RunState.FAILED),
    (RunState.WAITING, RunState.CANCELLED),
}
"""Every move a run may make, written out rather than read from the table."""

PAIRS = list(itertools.product(sorted(RunState), repeat=2))


@pytest.mark.parametrize(("current", "target"), PAIRS)
def test_every_pair_of_states_is_legal_or_it_is_not(current: RunState, target: RunState) -> None:
    assert may_transition(current, target) == ((current, target) in LEGAL)


def test_the_table_is_the_moves_written_out_here_and_no_others() -> None:
    assert set(RUN_TRANSITIONS) == set(RunState)
    assert {
        (current, target) for current, targets in RUN_TRANSITIONS.items() for target in targets
    } == LEGAL


@pytest.mark.parametrize(("current", "target"), PAIRS)
def test_an_illegal_move_is_refused_and_a_legal_one_is_made(
    current: RunState, target: RunState
) -> None:
    before = run(state=current) if current in ACTIVE_RUN_STATES else ended(current)
    error = "the provider said no" if target is RunState.FAILED else None
    if (current, target) not in LEGAL:
        with pytest.raises(IllegalTransitionError, match=target.value):
            check_transition(before, target)
        with pytest.raises(IllegalTransitionError):
            transition(before, target, now=at(5), error=error)
        return
    assert check_transition(before, target) is None
    after = transition(before, target, now=at(5), error=error)
    assert after.state is target
    assert after.id == before.id


def test_a_state_is_never_its_own_successor() -> None:
    for state in RunState:
        assert not may_transition(state, state)


@pytest.mark.parametrize("value", ["running", None, 7])
def test_a_transition_is_between_states(value: object) -> None:
    with pytest.raises(InvalidValueError):
        may_transition(RunState.RUNNING, value)
    with pytest.raises(InvalidValueError):
        may_transition(value, RunState.RUNNING)


def test_ending_a_run_stamps_when_it_ended() -> None:
    over = transition(run(), RunState.FINISHED, now=at(5))
    assert over.finished_at == at(5)
    assert over.error is None


def test_a_clock_that_stepped_backwards_does_not_disorder_a_run() -> None:
    """The record does not refuse to exist -- a run in flight would be lost --
    so what is written is clamped to the stamp it follows."""
    going = run(created_at=at(10), started_at=at(10))
    over = transition(going, RunState.FINISHED, now=at(3))
    assert over.finished_at == at(10)
    resumed = transition(
        run(state=RunState.WAITING, created_at=at(10), started_at=None), RunState.RUNNING, now=at(3)
    )
    assert resumed.started_at == at(10)
    ended_late = transition(resumed, RunState.CANCELLED, now=at(2))
    assert ended_late.finished_at == at(10)


def test_a_failure_keeps_what_went_wrong_and_a_good_end_has_none_to_keep() -> None:
    failed = transition(run(), RunState.FAILED, now=at(5), error="the provider said no")
    assert failed.error == "the provider said no"
    assert transition(run(), RunState.CANCELLED, now=at(5)).error is None
    # Not quietly dropped: a caller passing an error and a good end together
    # has chosen the wrong end, and hears about it.
    with pytest.raises(InvalidValueError, match="records no error"):
        transition(run(), RunState.FINISHED, now=at(5), error="the provider said no")


def test_ending_a_run_never_fails_over_the_error_text() -> None:
    """A failure that could not be written down would leave the run running
    for ever, which is the one outcome worse than a truncated message."""
    traceback = "x" * 5_000
    failed = transition(run(), RunState.FAILED, now=at(5), error=traceback)
    assert len(failed.error) == MAX_RUN_ERROR_CHARS
    assert failed.error.endswith(TRUNCATED)
    assert transition(run(), RunState.FAILED, now=at(5)).error == UNSAID_ERROR
    assert transition(run(), RunState.FAILED, now=at(5), error="  ").error == UNSAID_ERROR
    unstorable = transition(run(), RunState.FAILED, now=at(5), error="said\x00\ud800 no")
    assert unstorable.error == "said\ufffd no"
    stopped = transition(run(), RunState.CANCELLED, now=at(5), error="y" * 5_000)
    assert len(stopped.error) == MAX_RUN_ERROR_CHARS


@pytest.mark.parametrize(
    ("given", "kept"),
    [
        (None, UNSAID_ERROR),
        ("", UNSAID_ERROR),
        ("\n\t ", UNSAID_ERROR),
        (7, UNSAID_ERROR),
        ("the provider said no", "the provider said no"),
        ("a\x00b", "ab"),
        ("\ud83d" + "\ude00", "\U0001f600"),
    ],
)
def test_what_a_run_records_about_a_failure(given: object, kept: str) -> None:
    assert run_error(given) == kept


def test_a_run_that_ends_records_a_start_it_never_recorded() -> None:
    """An ended run with no beginning is a row nothing can measure."""
    over = transition(run(started_at=None), RunState.FINISHED, now=at(5))
    assert over.started_at == at(5)
    assert over.finished_at == at(5)
    waiting = transition(run(), RunState.WAITING, now=at(5))
    stopped = transition(replace(waiting, started_at=None), RunState.CANCELLED, now=at(6))
    assert stopped.started_at is None, "a run that never ran is not given a start"
    assert stopped.finished_at == at(6)


def test_suspending_and_resuming_keeps_the_start_and_leaves_the_end_open() -> None:
    waiting = transition(run(), RunState.WAITING, now=at(5))
    assert (waiting.started_at, waiting.finished_at) == (at(0), None)
    resumed = transition(waiting, RunState.RUNNING, now=at(9))
    assert resumed.started_at == at(0)
    assert resumed.is_active


def test_a_run_that_had_not_started_is_stamped_when_it_does() -> None:
    resumed = transition(run(state=RunState.WAITING, started_at=None), RunState.RUNNING, now=at(9))
    assert resumed.started_at == at(9)


# --- one active run per conversation ----------------------------------------


def test_a_conversation_with_nothing_going_may_start_a_run() -> None:
    assert active_run([]) is None
    assert may_start_run([])
    assert check_may_start_run([]) is None
    over = [ended(RunState.FINISHED), ended(RunState.CANCELLED), ended(RunState.FAILED)]
    assert may_start_run(over)


@pytest.mark.parametrize("state", [RunState.RUNNING, RunState.WAITING])
def test_a_conversation_already_answering_refuses_a_new_run(state: RunState) -> None:
    going = run(state=state)
    runs = [ended(RunState.FINISHED), going]
    assert active_run(runs) == going
    assert not may_start_run(runs)
    with pytest.raises(RunAlreadyActiveError, match=str(going.id)):
        check_may_start_run(runs)


def test_two_active_runs_are_reported_rather_than_chosen_between() -> None:
    with pytest.raises(InvalidValueError, match="at most one active run"):
        active_run([run(), run(id=uuid.uuid4(), conversation_id=CONVERSATION)])


def test_rows_of_ours_that_contradict_each_other_are_a_fault_of_ours() -> None:
    """The read path: two runs going at once is nothing a request did."""
    both = [run(), run(id=uuid.uuid4(), conversation_id=CONVERSATION)]
    for read in (active_run_stored, check_may_start_run_stored):
        with pytest.raises(StoredDataError) as refused:
            read(both)
        assert refused.value.__cause__ is not None
    going = run()
    assert active_run_stored([ended(RunState.FINISHED), going]) == going
    assert active_run_stored([]) is None
    with pytest.raises(RunAlreadyActiveError, match=str(going.id)):
        check_may_start_run_stored([going])
    assert check_may_start_run_stored([ended(RunState.FINISHED)]) is None


# --- what an engine yields ---------------------------------------------------


def answered(text: str = "Some one") -> tuple[object, ...]:
    """One answer, streamed in two pieces and completed."""
    return (
        AnswerStarted(),
        AnswerReasoningDelta(text="thinking"),
        AnswerTextDelta(text=text[:4]),
        AnswerTextDelta(text=text[4:]),
        AnswerCompleted(parts=(TextPart(text),)),
    )


def test_what_an_engine_yields_for_a_turn() -> None:
    assert check_engine_events(answered()) is None
    assert check_engine_events((*answered("one"), *answered("two"))) is None


CALL = ToolCallPart("toolu_01", "github__search", {"q": "robinauts"})


def asked_for_tools(*, streamed: bool = True) -> tuple[object, ...]:
    """One answer that calls a tool, and the turn ending waiting on it."""
    arguments: tuple[object, ...] = ()
    if streamed:
        arguments = (
            ToolCallArgumentsDelta(call_id=CALL.call_id, text='{"q": "robi'),
            ToolCallArgumentsDelta(call_id=CALL.call_id, text='nauts"}'),
        )
    return (
        AnswerStarted(),
        AnswerTextDelta(text="Let me look."),
        ToolCallStarted(call_id=CALL.call_id, name=CALL.name),
        *arguments,
        ToolCallCompleted(call=CALL),
        AnswerCompleted(parts=(TextPart("Let me look."), CALL)),
        WaitingOnTools(),
    )


def test_an_answer_that_asks_for_tools_ends_the_turn_waiting() -> None:
    assert check_engine_events(asked_for_tools()) is None
    assert check_engine_events(asked_for_tools(streamed=False)) is None
    # After answers that asked for none, and never before another answer.
    assert check_engine_events((*answered("first"), *asked_for_tools())) is None
    with pytest.raises(InvalidValueError, match="ends the turn waiting"):
        check_engine_events(asked_for_tools()[:-1])
    with pytest.raises(InvalidValueError, match="ends the turn waiting"):
        check_engine_events((*asked_for_tools()[:-1], *answered("then more")))
    with pytest.raises(InvalidValueError, match="nothing follows"):
        check_engine_events((*asked_for_tools(), *answered("then more")))
    # And a turn that asked for none never says it is waiting.
    with pytest.raises(InvalidValueError, match="waits on the tools"):
        check_engine_events((*answered(), WaitingOnTools()))
    with pytest.raises(InvalidValueError, match="waits on the tools"):
        check_engine_events((AnswerStarted(), WaitingOnTools()))
    # Cut short, a turn may stop before saying so: that is what a failure
    # between the answer and the waiting looks like.
    assert check_engine_events(asked_for_tools()[:-1], cut_short=True) is None


def test_a_tool_call_is_announced_inside_an_answer_one_at_a_time() -> None:
    started, said, announced, first, second, completed, answer, waiting = asked_for_tools()
    with pytest.raises(InvalidValueError, match="belongs to an answer"):
        check_engine_events((announced, started))
    with pytest.raises(InvalidValueError, match="one tool call at a time"):
        check_engine_events((started, announced, announced))
    other = ToolCallStarted(call_id="toolu_02", name="jira__find")
    with pytest.raises(InvalidValueError, match="one tool call at a time"):
        check_engine_events((started, announced, other))
    # The same id twice in one answer would answer nothing.
    twice = ToolCallCompleted(call=CALL)
    with pytest.raises(InvalidValueError, match="each tool call once"):
        check_engine_events((started, announced, twice, announced, twice, answer, waiting))
    with pytest.raises(InvalidValueError, match="that was announced is completed"):
        check_engine_events((started, announced, first, answer))
    with pytest.raises(InvalidValueError, match="completed once, after it was announced"):
        check_engine_events((started, completed))


def test_a_tool_calls_arguments_belong_to_the_open_call_and_are_what_it_completes_with() -> None:
    started, said, announced, first, second, completed, answer, waiting = asked_for_tools()
    with pytest.raises(InvalidValueError, match="belong to the tool call being made"):
        check_engine_events((started, first))
    stray = ToolCallArgumentsDelta(call_id="toolu_02", text="{}")
    with pytest.raises(InvalidValueError, match="belong to the tool call being made"):
        check_engine_events((started, announced, stray))
    with pytest.raises(InvalidValueError, match="are the arguments it completed with"):
        check_engine_events(
            (
                started,
                announced,
                first,
                ToolCallArgumentsDelta(call_id=CALL.call_id, text='x"}'),
                completed,
            )
        )
    with pytest.raises(InvalidValueError, match="are JSON"):
        check_engine_events(
            (
                started,
                announced,
                ToolCallArgumentsDelta(call_id=CALL.call_id, text="not json"),
                completed,
            )
        )
    # Whitespace alone is nothing streamed, as an empty text delta is.
    blank = ToolCallArgumentsDelta(call_id=CALL.call_id, text=" ")
    assert check_engine_events((started, announced, blank, completed, answer, waiting)) is None
    # A character cut in half across two deltas -- a surrogate pair the
    # vendor split -- is joined before it is read, as an answer's text is.
    high = ToolCallArgumentsDelta(call_id=CALL.call_id, text='{"q": "\ud83d')
    low = ToolCallArgumentsDelta(call_id=CALL.call_id, text='\ude00"}')
    smiled = ToolCallCompleted(call=ToolCallPart(CALL.call_id, CALL.name, {"q": "\U0001f600"}))
    whole = AnswerCompleted(parts=(TextPart("Let me look."), smiled.call))
    assert check_engine_events((started, announced, high, low, smiled, whole, waiting)) is None


def test_streamed_arguments_are_compared_as_json_and_refused_past_what_the_parser_follows() -> None:
    started, said, announced, first, second, completed, answer, waiting = asked_for_tools()
    # ``1`` and ``1.0`` are one value to Python and two to JSON; the record
    # holds what was streamed, so the wire and the store must agree as JSON.
    completed_as_int = ToolCallCompleted(call=ToolCallPart(CALL.call_id, CALL.name, {"n": 1}))
    streamed_as_float = ToolCallArgumentsDelta(call_id=CALL.call_id, text='{"n": 1.0}')
    with pytest.raises(InvalidValueError, match="are the arguments it completed with"):
        check_engine_events((started, announced, streamed_as_float, completed_as_int))
    streamed_as_bool = ToolCallArgumentsDelta(call_id=CALL.call_id, text='{"n": true}')
    with pytest.raises(InvalidValueError, match="are the arguments it completed with"):
        check_engine_events((started, announced, streamed_as_bool, completed_as_int))
    # Deltas are bounded one by one and not in number: text nested past what
    # the parser can follow is refused as not the call's JSON, not raised as
    # a RecursionError from a place that promised a refusal.
    deep = [ToolCallArgumentsDelta(call_id=CALL.call_id, text="[" * 5_000) for _ in range(40)]
    with pytest.raises(InvalidValueError, match="are JSON"):
        check_engine_events((started, announced, *deep, completed))


def test_a_tool_call_completes_as_it_was_announced_and_the_answer_holds_exactly_the_calls() -> None:
    started, said, announced, first, second, completed, answer, waiting = asked_for_tools()
    renamed = ToolCallCompleted(call=ToolCallPart(CALL.call_id, "other__tool", CALL.arguments))
    with pytest.raises(InvalidValueError, match="completed as it was announced"):
        check_engine_events((started, announced, renamed))
    # The completed answer holds the calls it announced, and no other.
    with pytest.raises(InvalidValueError, match="exactly the calls it announced"):
        check_engine_events(
            (
                started,
                announced,
                first,
                second,
                completed,
                AnswerCompleted(parts=(TextPart("Let me look."),)),
                waiting,
            )
        )
    with pytest.raises(InvalidValueError, match="exactly the calls it announced"):
        check_engine_events((AnswerStarted(), AnswerCompleted(parts=(CALL,)), WaitingOnTools()))


def test_a_turn_produces_an_answer_and_one_that_produces_none_failed() -> None:
    """A run that ends with no answer is a failed run, never a finished one,
    so a stream with nothing in it is refused rather than turned into a
    conversation with a turn missing from it."""
    with pytest.raises(InvalidValueError, match="one that produces none failed"):
        check_engine_events(())
    assert check_engine_events((), cut_short=True) is None
    # A turn that announced an answer and never finished it is cut short,
    # which is the failure case and not a turn that produced nothing.
    assert check_engine_events((AnswerStarted(),), cut_short=True) is None


def test_an_engine_produces_one_answer_at_a_time() -> None:
    started, *rest = answered()
    with pytest.raises(InvalidValueError, match="one answer at a time"):
        check_engine_events((started, started, *rest))


def test_an_answer_is_announced_before_its_deltas_and_completed_after_them() -> None:
    started, thinking, first, second, completed = answered()
    with pytest.raises(InvalidValueError, match="an answer being produced"):
        check_engine_events((first, started, second, completed))
    with pytest.raises(InvalidValueError, match="completed once"):
        check_engine_events((completed,))
    with pytest.raises(InvalidValueError, match="that was announced is completed"):
        check_engine_events((started, thinking, first, second))


def test_an_answer_that_streamed_nothing_may_complete_with_anything() -> None:
    """Not every provider streams, and not every path through one does."""
    assert (
        check_engine_events((AnswerStarted(), AnswerCompleted(parts=(TextPart("all at once"),))))
        is None
    )
    assert (
        check_engine_events(
            (
                AnswerStarted(),
                AnswerReasoningDelta(text="thinking"),
                AnswerCompleted(parts=(TextPart("all at once"),)),
            )
        )
        is None
    )


def test_a_turn_may_stream_one_answer_and_not_the_next() -> None:
    assert (
        check_engine_events(
            (*answered("one"), AnswerStarted(), AnswerCompleted(parts=(TextPart("two"),)))
        )
        is None
    )
    with pytest.raises(InvalidValueError, match="text deltas are the text"):
        check_engine_events(
            (
                AnswerStarted(),
                AnswerCompleted(parts=(TextPart("one"),)),
                AnswerStarted(),
                AnswerTextDelta(text="two"),
                AnswerCompleted(parts=(TextPart("something else"),)),
            )
        )


def test_a_turn_cut_short_is_checked_only_when_it_is_asked_for() -> None:
    """An engine reports a failure by raising and a cancellation closes it
    where it stands, so what it yielded ends wherever it ended."""
    cut = (AnswerStarted(), AnswerTextDelta(text="half an ans"))
    with pytest.raises(InvalidValueError, match="that was announced is completed"):
        check_engine_events(cut)
    assert check_engine_events(cut, cut_short=True) is None
    assert check_engine_events(answered(), cut_short=True) is None
    # Everything else still holds when a turn was cut short.
    with pytest.raises(InvalidValueError, match="one answer at a time"):
        check_engine_events((AnswerStarted(), AnswerStarted()), cut_short=True)


def test_reasoning_is_streamed_and_never_has_to_be_completed_with() -> None:
    """This version shows it as it arrives and keeps none of it."""
    assert (
        check_engine_events(
            (
                AnswerStarted(),
                AnswerReasoningDelta(text="a long thought"),
                AnswerTextDelta(text="Hi"),
                AnswerCompleted(parts=(TextPart("Hi"),)),
            )
        )
        is None
    )
    # And an engine that does return it is not refused: the application drops
    # it, because an engine is not asked to know what the platform keeps.
    assert (
        check_engine_events(
            (
                AnswerStarted(),
                AnswerTextDelta(text="Hi"),
                AnswerCompleted(parts=(ReasoningPart("a long thought"), TextPart("Hi"))),
            )
        )
        is None
    )


def test_what_an_engine_streamed_is_what_it_completed_with() -> None:
    """An engine that streamed one thing and returned another would be two
    engines, and a conversation started on one would not continue on the
    other."""
    started, thinking, first, second, _ = answered()
    with pytest.raises(InvalidValueError, match="text deltas are the text"):
        check_engine_events(
            (started, thinking, first, second, AnswerCompleted(parts=(TextPart("something else"),)))
        )
    # The pieces are joined before they are compared, so half a character
    # arriving on a boundary is not a difference.
    split = (
        AnswerStarted(),
        AnswerTextDelta(text="a\ud83d"),
        AnswerTextDelta(text="\ude00b"),
        AnswerCompleted(parts=(TextPart("a\U0001f600b"),)),
    )
    assert check_engine_events(split) is None


def test_reasoning_is_streamed_and_need_not_be_kept() -> None:
    """This version drops it; an engine that streams it is still in order."""
    assert (
        check_engine_events(
            (
                AnswerStarted(),
                AnswerReasoningDelta(text="thinking"),
                AnswerCompleted(parts=(TextPart(""),)),
            )
        )
        is None
    )


def test_an_engine_knows_nothing_of_runs_ids_or_rows() -> None:
    for event in (
        RunStarted(run_id=RUN, conversation_id=CONVERSATION),
        RunEnded(run_id=RUN, state=RunState.FINISHED),
        MessageStarted(run_id=RUN, message_id=uuid.uuid4(), parent_id=uuid.uuid4()),
    ):
        with pytest.raises(InvalidValueError, match="the application"):
            check_engine_events((event,))
    with pytest.raises(InvalidValueError, match="engine events"):
        check_engine_events(("an answer",))


# --- the order of a run's events --------------------------------------------


def produced(messages: int = 1) -> tuple[Message, list[Message], tuple[object, ...]]:
    """A whole run: begun, ``messages`` messages in a chain, ended."""
    asked = question()
    events: list[object] = [RunStarted(run_id=RUN, conversation_id=CONVERSATION)]
    said: list[Message] = []
    parent: Message = asked
    for step in range(messages):
        text = f"part {step}"
        made = answer(parent, text, seconds=step + 1)
        events.append(MessageStarted(run_id=RUN, message_id=made.id, parent_id=made.parent_id))
        # What is published is what is stored, so the delta is the text.
        events.append(TextDelta(run_id=RUN, message_id=made.id, text=text))
        events.append(ReasoningDelta(run_id=RUN, message_id=made.id, text="thinking"))
        events.append(MessageCompleted(run_id=RUN, message=made))
        said.append(made)
        parent = made
    events.append(RunEnded(run_id=RUN, state=RunState.FINISHED))
    return asked, said, tuple(events)


def stream(events: Iterable[object], *, after: int = 0) -> list[RunEvent]:
    """The events numbered as the application numbers them."""
    return [
        RunEvent(run_id=RUN, seq=after + FIRST_POSITION + at, event=event)
        for at, event in enumerate(events)
    ]


def ordered(events: Iterable[object], **changes: object) -> None:
    """``check_event_order`` with what every stream says about itself."""
    fields: dict[str, object] = {"run_id": RUN, "conversation_id": CONVERSATION}
    fields.update(changes)
    check_event_order(events, **fields)  # type: ignore[arg-type]


def test_a_whole_run_in_order_passes_numbered_or_not() -> None:
    asked, said, events = produced(2)
    assert ordered(events, follows=asked.id) is None
    assert ordered(stream(events), follows=asked.id) is None
    assert said[1].parent_id == said[0].id


def test_a_stream_says_which_run_which_conversation_and_what_to_follow() -> None:
    """Three of the guarantees; a caller that left one out would be checking
    less than it thinks, so none of them has a default."""
    asked, _, events = produced()
    for missing in ({"run_id": RUN}, {"conversation_id": CONVERSATION}, {"follows": asked.id}):
        with pytest.raises(TypeError):
            check_event_order(events, **missing)  # type: ignore[arg-type]
    with pytest.raises(InvalidValueError):
        ordered(events, follows=str(asked.id))
    with pytest.raises(InvalidValueError, match="belongs to the run it is of"):
        check_event_order(
            events, run_id=uuid.uuid4(), conversation_id=CONVERSATION, follows=asked.id
        )


def test_a_run_belongs_to_the_conversation_it_answers_in() -> None:
    asked, _, events = produced()
    elsewhere = (
        RunStarted(run_id=RUN, conversation_id=OTHER_CONVERSATION),
        *events[1:],
    )
    with pytest.raises(InvalidValueError, match="belongs to the conversation"):
        ordered(elsewhere, follows=asked.id)


def test_a_run_begins_once_before_anything_else_and_ends_once_at_the_end() -> None:
    asked, _, events = produced()
    with pytest.raises(InvalidValueError):
        ordered(events[1:], follows=asked.id)
    with pytest.raises(InvalidValueError):
        ordered(events[:-1], follows=asked.id)
    with pytest.raises(InvalidValueError, match="ends with the event that ends the run"):
        ordered((*events, RunEnded(run_id=RUN, state=RunState.FINISHED)), follows=asked.id)
    with pytest.raises(InvalidValueError, match="starts once"):
        ordered((events[0], *events), follows=asked.id)


def test_a_run_that_finished_produced_a_message_and_left_none_half_written() -> None:
    asked, _, events = produced()
    begun, announced, delta, thinking, completed, over = events
    with pytest.raises(InvalidValueError, match="left no message half-written"):
        ordered((begun, announced, delta, thinking, over), follows=asked.id)
    with pytest.raises(InvalidValueError, match="finished produced a message"):
        ordered((begun, over), follows=asked.id)


@pytest.mark.parametrize("state", [RunState.FAILED, RunState.CANCELLED, RunState.INTERRUPTED])
def test_a_run_that_ended_badly_may_leave_a_message_unfinished(state: RunState) -> None:
    asked, _, events = produced()
    begun, announced, delta, *_ = events
    error = "the provider said no" if state is RunState.FAILED else None
    badly = RunEnded(run_id=RUN, state=state, error=error)
    assert ordered((begun, announced, delta, badly), follows=asked.id) is None
    assert ordered((begun, badly), follows=asked.id) is None


def test_a_delta_belongs_to_the_message_being_produced() -> None:
    asked, _, events = produced()
    begun, announced, delta, thinking, completed, over = events
    with pytest.raises(InvalidValueError, match="a message being produced"):
        ordered((begun, delta, announced, thinking, completed, over), follows=asked.id)
    with pytest.raises(InvalidValueError, match="a message being produced"):
        ordered((begun, announced, thinking, completed, delta, over), follows=asked.id)
    elsewhere = TextDelta(run_id=RUN, message_id=uuid.uuid4(), text="Some")
    with pytest.raises(InvalidValueError, match="a message being produced"):
        ordered((begun, announced, elsewhere, completed, over), follows=asked.id)


def test_a_run_produces_one_message_at_a_time() -> None:
    """A turn is a chain: it never forks, and nothing is interleaved."""
    asked, _, events = produced(2)
    begun, first_start, *rest = events
    second_start = events[5]
    with pytest.raises(InvalidValueError, match="one message at a time"):
        ordered((begun, first_start, second_start, *rest), follows=asked.id)
    forked = MessageStarted(run_id=RUN, message_id=uuid.uuid4(), parent_id=first_start.parent_id)
    with pytest.raises(InvalidValueError, match="one message at a time"):
        ordered((begun, first_start, forked, *rest), follows=asked.id)


def test_a_message_is_announced_once_and_completed_once() -> None:
    asked, _, events = produced()
    begun, announced, delta, thinking, completed, over = events
    with pytest.raises(InvalidValueError, match="announced once"):
        ordered((begun, announced, delta, thinking, completed, announced, over), follows=asked.id)
    with pytest.raises(InvalidValueError, match="completed once"):
        ordered((begun, announced, delta, thinking, completed, completed, over), follows=asked.id)


def test_each_message_hangs_under_the_one_before_it() -> None:
    asked, said, events = produced(2)
    with pytest.raises(InvalidValueError, match="in a chain"):
        ordered(events, follows=uuid.uuid4())
    loose = MessageStarted(run_id=RUN, message_id=said[1].id, parent_id=asked.id)
    with pytest.raises(InvalidValueError, match="in a chain"):
        ordered((*events[:5], loose, *events[6:]), follows=asked.id)


def test_a_completed_message_is_the_one_that_was_announced() -> None:
    asked, said, events = produced()
    begun, announced, delta, thinking, completed, over = events
    elsewhere = MessageCompleted(run_id=RUN, message=answer(asked, "elsewhere", seconds=9))
    with pytest.raises(InvalidValueError, match="completed once"):
        ordered((begun, announced, delta, thinking, elsewhere, over), follows=asked.id)
    moved = MessageCompleted(run_id=RUN, message=replace(said[0], parent_id=uuid.uuid4()))
    with pytest.raises(InvalidValueError, match="as it was announced"):
        ordered((begun, announced, delta, thinking, moved, over), follows=asked.id)


def test_what_was_published_for_a_message_is_the_message_that_was_stored() -> None:
    """The platform's half of the promise the engines are held to: a watcher
    that saw an answer arrive has the answer that is in the conversation."""
    asked, said, events = produced()
    begun, announced, delta, thinking, completed, over = events
    forgotten = MessageCompleted(run_id=RUN, message=replace_text(said[0], said[0].text + "!"))
    with pytest.raises(InvalidValueError, match="is the message that was stored"):
        # The last piece was never published: a flush nobody wrote.
        ordered((begun, announced, delta, thinking, forgotten, over), follows=asked.id)
    rewritten = TextDelta(run_id=RUN, message_id=said[0].id, text="something else")
    with pytest.raises(InvalidValueError, match="is the message that was stored"):
        ordered((begun, announced, rewritten, thinking, completed, over), follows=asked.id)
    # A message nothing was published for may be stored whole: not every
    # answer is streamed, and a delta that carried nothing is nothing
    # published -- as on the engine's side of the same promise.
    assert ordered((begun, announced, thinking, completed, over), follows=asked.id) is None
    nothing = TextDelta(run_id=RUN, message_id=said[0].id, text="")
    assert ordered((begun, announced, nothing, thinking, completed, over), follows=asked.id) is None


def test_a_slice_that_began_inside_a_message_compares_nothing_for_that_one() -> None:
    """It did not see the deltas before the cut, so it cannot add them up."""
    asked, said, events = produced()
    rest = stream(events)[3:]  # the run, the announcement and the delta are behind it
    assert ordered(rest, after=3, follows=asked.id, open_message=said[0].id) is None


def test_a_run_produces_answers_of_its_own_conversation_and_its_own_run() -> None:
    asked, said, events = produced()
    begun, announced, delta, thinking, _, over = events
    asking = MessageCompleted(
        run_id=RUN, message=question(parent=asked.parent_id, id=said[0].id, seconds=1)
    )
    with pytest.raises(InvalidValueError, match="answers, not questions"):
        ordered((begun, announced, delta, thinking, asking, over), follows=asked.id)

    elsewhere = MessageCompleted(
        run_id=RUN, message=replace(said[0], conversation_id=OTHER_CONVERSATION)
    )
    with pytest.raises(InvalidValueError, match="its own conversation"):
        ordered((begun, announced, delta, thinking, elsewhere, over), follows=asked.id)

    another_run = MessageCompleted(
        run_id=RUN, message=replace(said[0], provenance=provenance(run_id=uuid.uuid4()))
    )
    with pytest.raises(InvalidValueError, match="records that run"):
        ordered((begun, announced, delta, thinking, another_run, over), follows=asked.id)


def test_the_positions_start_at_the_first_one_and_have_no_gaps() -> None:
    asked, _, events = produced()
    numbered = stream(events)
    with pytest.raises(InvalidValueError, match="no gaps"):
        ordered(numbered[1:], follows=asked.id)
    with pytest.raises(InvalidValueError, match="no gaps"):
        ordered([numbered[0], *numbered[2:]], follows=asked.id)
    with pytest.raises(InvalidValueError, match="all numbered, or none"):
        ordered([numbered[0], *events[1:]], follows=asked.id)


def test_a_long_answer_is_checked_in_one_pass() -> None:
    """An answer's events are one per token: a check that looked at every
    other event for each of them would take seconds on a long one."""
    asked = question()
    said = answer(asked, "x" * 50_000, seconds=1)
    events: list[object] = [
        RunStarted(run_id=RUN, conversation_id=CONVERSATION),
        MessageStarted(run_id=RUN, message_id=said.id, parent_id=said.parent_id),
    ]
    events.extend(TextDelta(run_id=RUN, message_id=said.id, text="x") for _ in range(50_000))
    events.append(MessageCompleted(run_id=RUN, message=said))
    events.append(RunEnded(run_id=RUN, state=RunState.FINISHED))

    started = time.monotonic()
    assert ordered(stream(events), follows=asked.id) is None
    spent = time.monotonic() - started
    assert spent < 2.0, f"fifty thousand events took {spent:.1f}s"


# --- re-attaching -----------------------------------------------------------


def test_a_watcher_carries_on_from_the_position_it_last_saw() -> None:
    """The slice a re-attaching watcher receives: numbered from where it left
    off, beginning in the middle of the message being produced."""
    asked, said, events = produced()
    numbered = stream(events, after=17)
    assert numbered[0].seq == 18
    rest = numbered[2:]  # it saw the run start and the message announced
    assert ordered(rest, after=19, follows=asked.id, open_message=said[0].id) is None
    assert ordered(rest, after=19, follows=asked.id) is None, "the open message is inferred"


def test_a_slice_carries_on_from_its_own_position_and_no_other() -> None:
    asked, said, events = produced()
    rest = stream(events, after=17)[2:]
    with pytest.raises(InvalidValueError, match="numbered from 19"):
        ordered(rest, after=18, follows=asked.id, open_message=said[0].id)
    with pytest.raises(InvalidValueError, match="numbered from 21"):
        ordered(rest, after=20, follows=asked.id, open_message=said[0].id)


def test_a_slice_that_began_between_messages_is_checked_like_any_other() -> None:
    """It did not begin inside a message, so what the next one follows is
    known, and a wrong parent is a wrong parent."""
    asked, said, events = produced()
    rest = stream(events)[1:]  # it saw the run start and no more
    assert ordered(rest, after=1, follows=asked.id) is None
    with pytest.raises(InvalidValueError, match="in a chain"):
        ordered(rest, after=1, follows=uuid.uuid4())


def test_a_slice_never_carries_the_start_of_the_run() -> None:
    asked, _, events = produced()
    with pytest.raises(InvalidValueError, match="starts once"):
        ordered(stream(events, after=17), after=17, follows=asked.id)


def test_a_slice_is_numbered_or_it_cannot_be_placed() -> None:
    asked, _, events = produced()
    with pytest.raises(InvalidValueError, match="is numbered"):
        ordered(events[2:], after=2, follows=asked.id)


def test_a_slice_begins_inside_one_message_and_only_one() -> None:
    asked, _, events = produced(2)
    rest = stream(events, after=2)[2:]
    with pytest.raises(InvalidValueError, match="a message being produced"):
        ordered(rest, after=4, follows=asked.id, open_message=uuid.uuid4())


def test_only_a_slice_begins_inside_a_message() -> None:
    asked, said, events = produced()
    with pytest.raises(InvalidValueError, match="only a slice"):
        ordered(events, follows=asked.id, open_message=said[0].id)


def test_where_a_watcher_opening_a_conversation_attaches() -> None:
    """It has the messages that are stored; what it must not be shown again
    is the events of those, and what it must not miss is the one in flight."""
    asked, said, events = produced(2)
    numbered = stream(events)
    whole = resume_point(numbered, answering=asked.id)
    assert whole.after == 9, "the position of the last completion"
    assert whole.follows == said[1].id
    begun = resume_point(numbered[:3], answering=asked.id)
    assert (begun.after, begun.follows) == (1, asked.id), "nothing completed yet"
    assert resume_point([], answering=asked.id) == ResumePoint(after=0, follows=asked.id)

    # Attaching there begins exactly at the next message's announcement, so
    # nothing is replayed and nothing is missed -- and the point says what
    # that announcement must hang under.
    point = resume_point(numbered[:6], answering=asked.id)
    assert point.follows == said[0].id
    slice_ = numbered[point.after :]
    assert isinstance(slice_[0].event, MessageStarted)
    assert ordered(slice_, after=point.after, follows=point.follows) is None
    with pytest.raises(InvalidValueError, match="in a chain"):
        ordered(slice_, after=point.after, follows=uuid.uuid4())
    assert said[1].parent_id == said[0].id


def test_one_parameter_says_what_the_next_message_follows() -> None:
    """The same thing whether the stream is a whole run or a slice of one."""
    asked, _, events = produced()
    numbered = stream(events)
    assert ordered(events, follows=asked.id) is None
    assert ordered(numbered[1:], after=1, follows=asked.id) is None


def test_attaching_to_a_run_whose_last_message_is_done_begins_at_its_end() -> None:
    """The slice a watcher gets never begins inside a message: it begins at
    the next announcement, or at the event that ends the run."""
    asked, _, events = produced()
    numbered = stream(events)
    point = resume_point(numbered, answering=asked.id)
    slice_ = numbered[point.after :]
    assert isinstance(slice_[0].event, RunEnded)
    assert ordered(slice_, after=point.after, follows=point.follows) is None


def test_attaching_to_a_run_that_has_completed_nothing_replays_its_one_message() -> None:
    asked, said, events = produced()
    numbered = stream(events)
    point = resume_point(numbered[:3], answering=asked.id)
    slice_ = numbered[point.after :]
    assert isinstance(slice_[0].event, MessageStarted)
    assert ordered(slice_, after=point.after, follows=point.follows) is None


def test_a_position_to_attach_after_is_taken_from_numbered_events() -> None:
    _, _, events = produced()
    with pytest.raises(InvalidValueError, match="are numbered"):
        resume_point(list(events), answering=uuid.uuid4())


@pytest.mark.parametrize("after", [-1, 1.5, True, "3", None])
def test_a_position_to_carry_on_from_is_a_whole_number(after: object) -> None:
    asked, _, events = produced()
    with pytest.raises(InvalidValueError, match="carry on from"):
        ordered(events, follows=asked.id, after=after)


def test_a_run_that_is_still_going_is_checked_without_its_end() -> None:
    """What a watcher attaching to a live run receives, and what a store
    holds for one whose end has not been written yet."""
    asked, said, events = produced()
    begun, announced, delta, thinking, completed, over = events
    live = (begun, announced, delta, thinking)
    assert ordered(live, follows=asked.id, ended=False) is None
    assert (
        ordered((begun, announced, delta, thinking, completed), follows=asked.id, ended=False)
        is None
    )
    with pytest.raises(InvalidValueError, match="starts with its run and ends with it"):
        ordered(live, follows=asked.id)
    with pytest.raises(InvalidValueError, match="still going has not ended"):
        ordered(events, follows=asked.id, ended=False)
    # A slice of a live run, the same way, and a live run that has published
    # nothing at all is a run that has not begun to answer yet.
    assert ordered(stream(events)[1:-1], after=1, follows=asked.id, ended=False) is None
    assert ordered((), follows=asked.id, ended=False) is None
    with pytest.raises(InvalidValueError, match="starts with its run"):
        ordered((), follows=asked.id)


@pytest.mark.parametrize("open_message", ["not a uuid", 7, object()])
def test_the_message_a_slice_begins_inside_is_an_id(open_message: object) -> None:
    asked, _, events = produced()
    with pytest.raises(InvalidValueError, match="a slice begins inside"):
        ordered(stream(events, after=2)[2:], after=4, follows=asked.id, open_message=open_message)


def test_a_refusal_about_a_position_does_not_repeat_it() -> None:
    asked, _, events = produced()
    with pytest.raises(InvalidValueError) as refused:
        ordered(events, follows=asked.id, after="a" * 500)
    assert "a" * 500 not in str(refused.value)


def test_every_event_of_a_stream_belongs_to_one_run() -> None:
    asked, _, events = produced()
    begun, announced, *rest = events
    elsewhere = uuid.uuid4()
    with pytest.raises(InvalidValueError, match="belongs to the run it is of"):
        ordered(
            (
                begun,
                MessageStarted(
                    run_id=elsewhere,
                    message_id=announced.message_id,
                    parent_id=announced.parent_id,
                ),
                *rest,
            ),
            follows=asked.id,
        )


def test_a_stream_carries_turn_events() -> None:
    with pytest.raises(InvalidValueError, match="turn events"):
        ordered(["run started"], follows=uuid.uuid4())


def replace_text(message: Message, text: str) -> Message:
    """The same message, saying something else."""
    return replace(message, parts=(TextPart(text),))


def generated_run(rng: random.Random) -> tuple[Message, list[Message], list[RunEvent]]:
    """A run as they really come: any end, streamed or not, whole or cut.

    A finished run completes every message it announced; one that failed, was
    cancelled or was interrupted may leave the last one open, which is what
    a cancellation in the middle of an answer looks like.
    """
    state = rng.choice(sorted(ENDED_RUN_STATES))
    asked = question(seconds=0)
    events: list[object] = [RunStarted(run_id=RUN, conversation_id=CONVERSATION)]
    said: list[Message] = []
    parent: Message = asked
    messages = rng.randint(1, 4) if state is RunState.FINISHED else rng.randint(0, 3)
    left_open = state is not RunState.FINISHED and rng.random() < 0.5
    for step in range(messages):
        text = "".join(rng.choice("abcdefg") for _ in range(rng.randint(0, 8)))
        made = answer(parent, text, seconds=step + 1)
        events.append(MessageStarted(run_id=RUN, message_id=made.id, parent_id=made.parent_id))
        if rng.random() < 0.5:
            events.append(ReasoningDelta(run_id=RUN, message_id=made.id, text="thinking"))
        # Streamed piece by piece, or not streamed at all: both are answers.
        if rng.random() < 0.7:
            for piece in text:
                events.append(TextDelta(run_id=RUN, message_id=made.id, text=piece))
        if left_open and step == messages - 1:
            break
        events.append(MessageCompleted(run_id=RUN, message=made))
        said.append(made)
        parent = made
    error = "the provider said no" if state is RunState.FAILED else None
    events.append(RunEnded(run_id=RUN, state=state, error=error))
    return asked, said, stream(events)


def test_every_prefix_of_a_run_can_be_attached_to_after() -> None:
    """The promise, over generated runs: whatever a watcher has seen, the
    point it is given carries on from there, passes the order check, and the
    messages it already had plus the ones the slice completes are the run's
    messages, each exactly once.
    """
    rng = random.Random(20260921)
    prefixes = 0
    seen_states: set[RunState] = set()
    seen_open = 0
    for _ in range(60):
        asked, said, numbered = generated_run(rng)
        over = numbered[-1].event
        seen_states.add(over.state)
        announced = {
            event.event.message_id for event in numbered if isinstance(event.event, MessageStarted)
        }
        seen_open += len(announced) > len(said)
        for cut in range(len(numbered) + 1):
            prefixes += 1
            point = resume_point(numbered[:cut], answering=asked.id)
            rest = numbered[point.after :]
            assert ordered(rest, after=point.after, follows=point.follows) is None
            had = [
                event.event.message
                for event in numbered[: point.after]
                if isinstance(event.event, MessageCompleted)
            ]
            arriving = [
                event.event.message for event in rest if isinstance(event.event, MessageCompleted)
            ]
            assert had + arriving == said
            assert len({message.id for message in had + arriving}) == len(said)
    assert prefixes > 400, prefixes
    # The generator is worth its seed only if it really varies.
    assert seen_states == ENDED_RUN_STATES, seen_states
    assert seen_open > 5, seen_open
