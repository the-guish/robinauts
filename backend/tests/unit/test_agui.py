# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The one mapping from the platform's turn events to AG-UI's.

Pure: a mapper is fed the events of one run and asked what it answers. What
the routes then do with them -- the heartbeat, the ending of a stream that
holds no ending of the run's own -- is ``test_stream_routes.py``.

Two things are asked of **every** event here, because they are what a wire
gets wrong quietly: that nothing of the platform's own record reaches the wire
but what the mapping names -- no ``raw_event``, no ``metadata``, no stored
error text -- and that the set of platform events which have a mapping is the
whole closed set ``domain.TurnEvent`` names, so an event kind added later
cannot be forgotten here.
"""

from __future__ import annotations

import json
import uuid
from typing import get_args

import pytest
from ag_ui.core import EventType, TextMessageStartEvent
from ag_ui.encoder import EventEncoder

from conversations import CONVERSATION, RUN, answer, ended
from robinauts.api import (
    ENDED_BADLY,
    REASONING_SUFFIX,
    SENT_ROLE,
    SSE_MEDIA_TYPE,
    UNMAPPED,
    UNSENDABLE_ROLE,
    AguiMapper,
    reasoning_id,
    sent_role,
    sse,
)
from robinauts.domain import (
    ENDED_RUN_STATES,
    FAULTED_RUN_STATES,
    ArgumentsDelta,
    CallCompleted,
    CallStarted,
    InvalidValueError,
    MessageCompleted,
    MessageStarted,
    ReasoningDelta,
    ResultLanded,
    RobinautsError,
    Role,
    RunEnded,
    RunEvent,
    RunStarted,
    RunState,
    TextDelta,
    TurnEvent,
)

MESSAGE = uuid.UUID("55555555-5555-4555-8555-555555555555")
"""The answer every stream here produces."""

QUESTION = uuid.UUID("66666666-6666-4666-8666-666666666666")
"""What it hangs under."""

SECRET = "psycopg.OperationalError: password=hunter2 refused by 10.0.0.3"
"""A stored error of the shape one really has: an operator's, not a browser's."""


def mapper() -> AguiMapper:
    return AguiMapper(thread_id=CONVERSATION)


def numbered(*events: TurnEvent) -> list[RunEvent]:
    """Those events, numbered from 1 as the application numbers them."""
    return [RunEvent(run_id=RUN, seq=seq, event=event) for seq, event in enumerate(events, 1)]


def through(*events: TurnEvent) -> list[dict[str, object]]:
    """What one mapper answers for that sequence, as the bodies that go out."""
    one = mapper()
    return [
        json.loads(sent.model_dump_json(by_alias=True))
        for event in numbered(*events)
        for sent in one.of(event)
    ]


def types(bodies: list[dict[str, object]]) -> list[str]:
    return [str(body["type"]) for body in bodies]


def started() -> RunStarted:
    return RunStarted(run_id=RUN, conversation_id=CONVERSATION)


def announced() -> MessageStarted:
    return MessageStarted(run_id=RUN, message_id=MESSAGE, parent_id=QUESTION)


def text(said: str) -> TextDelta:
    return TextDelta(run_id=RUN, message_id=MESSAGE, text=said)


def thought(said: str) -> ReasoningDelta:
    return ReasoningDelta(run_id=RUN, message_id=MESSAGE, text=said)


def completed() -> MessageCompleted:
    return MessageCompleted(run_id=RUN, message=answer(QUESTION, id=MESSAGE))


# --- one event at a time ------------------------------------------------------


def test_a_run_that_started_names_the_conversation_as_the_thread() -> None:
    (begun,) = through(started())

    assert begun == {
        "type": EventType.RUN_STARTED.value,
        "threadId": str(CONVERSATION),
        "runId": str(RUN),
    }


def test_a_message_that_started_is_a_text_message_with_its_role() -> None:
    begun, announcing = through(started(), announced())

    assert begun["type"] == EventType.RUN_STARTED.value
    assert announcing == {
        "type": EventType.TEXT_MESSAGE_START.value,
        "messageId": str(MESSAGE),
        "role": "assistant",
    }


def test_text_is_a_delta_of_the_message_it_belongs_to() -> None:
    (said,) = through(text("Someone "))

    assert said == {
        "type": EventType.TEXT_MESSAGE_CONTENT.value,
        "messageId": str(MESSAGE),
        "delta": "Someone ",
    }


def test_a_completed_message_is_an_end_and_nothing_of_what_was_stored() -> None:
    """The client built the message out of the deltas; the store has the rest."""
    (ending,) = through(completed())

    assert ending == {"type": EventType.TEXT_MESSAGE_END.value, "messageId": str(MESSAGE)}
    assert "Someone who plays fair." not in json.dumps(ending)


def test_a_run_that_finished_is_run_finished() -> None:
    (ended,) = through(RunEnded(run_id=RUN, state=RunState.FINISHED))

    assert ended == {
        "type": EventType.RUN_FINISHED.value,
        "threadId": str(CONVERSATION),
        "runId": str(RUN),
    }


# --- endings ------------------------------------------------------------------


@pytest.mark.parametrize("state", sorted(ENDED_BADLY, key=lambda one: one.value))
def test_a_run_that_ended_in_an_error_says_which_and_never_why(state: RunState) -> None:
    """One fixed sentence per state, and the state as the code to branch on."""
    (ended,) = through(RunEnded(run_id=RUN, state=state, error=SECRET))

    assert ended == {
        "type": EventType.RUN_ERROR.value,
        "message": ENDED_BADLY[state],
        "code": state.value,
    }


def test_a_cancelled_run_finished_with_a_cancelled_outcome_and_is_no_error() -> None:
    """AG-UI 1.0 has an outcome for it, and a stock client shows an error without it.

    "Stopped before it completed, by whoever was running it, and did not
    fail" -- which is what pressing stop is, and not something to show
    somebody as a failure.
    """
    (ended,) = through(RunEnded(run_id=RUN, state=RunState.CANCELLED))

    assert ended == {
        "type": EventType.RUN_FINISHED.value,
        "threadId": str(CONVERSATION),
        "runId": str(RUN),
        "outcome": {"type": "cancelled"},
    }
    assert RunState.CANCELLED not in ENDED_BADLY


def test_what_the_run_recorded_of_the_failure_is_nowhere_on_the_wire() -> None:
    """The stored error is written for an operator (``core.run_error``)."""
    sending = through(
        started(), announced(), RunEnded(run_id=RUN, state=RunState.FAILED, error=SECRET)
    )

    assert "hunter2" not in json.dumps(sending)
    assert "10.0.0.3" not in json.dumps(sending)
    assert "psycopg" not in json.dumps(sending)


def test_a_run_read_from_its_record_ends_the_stream_the_same_way() -> None:
    """``closed`` is for a slice that held no ending: one mapping, not two."""
    finished = mapper().closed(ended(RunState.FINISHED))
    failed = mapper().closed(ended(RunState.FAILED))

    assert json.loads(finished[0].model_dump_json(by_alias=True)) == {
        "type": EventType.RUN_FINISHED.value,
        "threadId": str(CONVERSATION),
        "runId": str(RUN),
    }
    assert json.loads(failed[0].model_dump_json(by_alias=True)) == {
        "type": EventType.RUN_ERROR.value,
        "message": ENDED_BADLY[RunState.FAILED],
        "code": RunState.FAILED.value,
    }
    # The record carries what went wrong, and it stays in the record.
    assert "the provider said no" not in json.dumps(
        [json.loads(one.model_dump_json(by_alias=True)) for one in failed]
    )


def test_a_run_read_from_its_record_closes_the_thinking_first() -> None:
    one = mapper()
    one.of(RunEvent(run_id=RUN, seq=1, event=thought("Hmm.")))

    closed = one.closed(ended(RunState.CANCELLED))

    assert [event.type.value for event in closed] == [
        EventType.REASONING_MESSAGE_END.value,
        EventType.RUN_FINISHED.value,
    ]


def test_every_way_a_run_can_end_has_an_event() -> None:  # noqa: D401 - a statement, not a verb
    """The table is the two that are errors; the mapping covers all four.

    A state added to ``ENDED_RUN_STATES`` without a word here would raise in
    the middle of a stream, so it is the **ended** states that are asked
    about, and ``ENDED_BADLY`` is what is left once the two that are not
    errors are taken out.
    """
    for state in ENDED_RUN_STATES:
        (ending,) = mapper().closed(ended(state))
        assert ending.type.value in {EventType.RUN_FINISHED.value, EventType.RUN_ERROR.value}
    assert set(ENDED_BADLY) == set(FAULTED_RUN_STATES) - {RunState.CANCELLED}


# --- thinking -----------------------------------------------------------------


def test_thinking_opens_a_reasoning_message_of_its_own_and_closes_before_the_text() -> None:
    sending = through(
        announced(), thought("Let me "), thought("see."), text("Someone"), completed()
    )

    assert types(sending) == [
        EventType.TEXT_MESSAGE_START.value,
        EventType.REASONING_MESSAGE_START.value,
        EventType.REASONING_MESSAGE_CONTENT.value,
        EventType.REASONING_MESSAGE_CONTENT.value,
        EventType.REASONING_MESSAGE_END.value,
        EventType.TEXT_MESSAGE_CONTENT.value,
        EventType.TEXT_MESSAGE_END.value,
    ]
    thinking = [body for body in sending if "REASONING" in str(body["type"])]
    # ``through`` numbers from 1, so the stretch opened at position 2.
    assert {str(body["messageId"]) for body in thinking} == {reasoning_id(MESSAGE, 2)}
    assert reasoning_id(MESSAGE, 2) == f"{MESSAGE}{REASONING_SUFFIX}2"
    assert reasoning_id(MESSAGE, 2) != str(MESSAGE)


def test_each_stretch_of_thinking_is_a_message_of_its_own() -> None:
    """An answer may think, say something and think again; two messages, not one.

    One id for both would open a reasoning message a client has already been
    told is over, which is the one thing a client cannot make sense of.
    """
    sending = through(announced(), thought("Hmm."), text("Some"), thought("Wait."), text("one"))

    thinking = [body for body in sending if "REASONING" in str(body["type"])]
    opened = [str(body["messageId"]) for body in thinking]
    assert opened == [
        reasoning_id(MESSAGE, 2),
        reasoning_id(MESSAGE, 2),
        reasoning_id(MESSAGE, 2),
        reasoning_id(MESSAGE, 4),
        reasoning_id(MESSAGE, 4),
        reasoning_id(MESSAGE, 4),
    ]
    assert len(set(opened)) == 2


def test_thinking_that_nothing_followed_is_closed_by_the_message_completing() -> None:
    sending = through(announced(), thought("Hmm."), completed())

    assert types(sending) == [
        EventType.TEXT_MESSAGE_START.value,
        EventType.REASONING_MESSAGE_START.value,
        EventType.REASONING_MESSAGE_CONTENT.value,
        EventType.REASONING_MESSAGE_END.value,
        EventType.TEXT_MESSAGE_END.value,
    ]


def test_thinking_is_closed_by_the_run_ending_under_it() -> None:
    """A turn that failed while it was thinking leaves no block open."""
    sending = through(
        announced(), thought("Hmm."), RunEnded(run_id=RUN, state=RunState.FAILED, error="no")
    )

    assert types(sending)[-2:] == [
        EventType.REASONING_MESSAGE_END.value,
        EventType.RUN_ERROR.value,
    ]


def test_thinking_is_closed_by_the_next_answer_being_announced() -> None:
    """A turn may hold several answers; the thinking of one is not the next's."""
    second = MessageStarted(run_id=RUN, message_id=uuid.uuid4(), parent_id=MESSAGE)
    sending = through(announced(), thought("Hmm."), second)

    assert types(sending) == [
        EventType.TEXT_MESSAGE_START.value,
        EventType.REASONING_MESSAGE_START.value,
        EventType.REASONING_MESSAGE_CONTENT.value,
        EventType.REASONING_MESSAGE_END.value,
        EventType.TEXT_MESSAGE_START.value,
    ]


def test_a_second_stretch_of_thinking_opens_a_message_again() -> None:
    sending = through(announced(), thought("Hmm."), text("Some"), thought("Wait."), text("one"))

    assert types(sending) == [
        EventType.TEXT_MESSAGE_START.value,
        EventType.REASONING_MESSAGE_START.value,
        EventType.REASONING_MESSAGE_CONTENT.value,
        EventType.REASONING_MESSAGE_END.value,
        EventType.TEXT_MESSAGE_CONTENT.value,
        EventType.REASONING_MESSAGE_START.value,
        EventType.REASONING_MESSAGE_CONTENT.value,
        EventType.REASONING_MESSAGE_END.value,
        EventType.TEXT_MESSAGE_CONTENT.value,
    ]


def test_closing_is_nothing_when_no_thinking_is_open() -> None:
    one = mapper()

    assert one.closing() == ()
    assert one.of(numbered(announced())[0]) != ()
    assert one.closing() == ()


# --- what is skipped ----------------------------------------------------------


def test_an_empty_delta_is_not_sent_at_all() -> None:
    """It appends nothing, and a client that renders each delta does nothing."""
    assert through(text("")) == []


def test_an_empty_piece_of_thinking_opens_nothing() -> None:
    sending = through(announced(), thought(""), completed())

    assert types(sending) == [
        EventType.TEXT_MESSAGE_START.value,
        EventType.TEXT_MESSAGE_END.value,
    ]


def test_no_event_carries_a_raw_event_or_metadata() -> None:
    """Where a provider's own payload would otherwise reach a browser."""
    sending = through(
        started(),
        announced(),
        thought("Hmm."),
        text("Someone"),
        completed(),
        RunEnded(run_id=RUN, state=RunState.FINISHED),
    )

    for body in sending:
        assert "rawEvent" not in body
        assert "metadata" not in body
        assert "raw_event" not in body


# --- the roles the two vocabularies share -------------------------------------


def test_the_roles_that_cross_are_the_ones_ag_ui_has_a_word_for() -> None:
    """``tool`` is a role of ours and none of AG-UI's, and it is not invented."""
    assert set(SENT_ROLE) == {Role.ASSISTANT, Role.USER}
    assert Role.TOOL not in SENT_ROLE
    assert sent_role(Role.ASSISTANT) == "assistant"
    # Every word this maps to is one AG-UI's own model takes.
    for word in SENT_ROLE.values():
        assert TextMessageStartEvent(message_id="m", role=word).role == word


@pytest.mark.parametrize("role", [Role.TOOL, "assistant", None])
def test_a_role_with_no_word_in_ag_ui_stops_the_stream_rather_than_crossing(
    role: object,
) -> None:
    """It ends the stream as ``internal`` and is logged; it invents nothing."""
    with pytest.raises(InvalidValueError) as raised:
        sent_role(role)  # type: ignore[arg-type]

    assert UNSENDABLE_ROLE in str(raised.value)


# --- the closed set -----------------------------------------------------------


NOT_MAPPED_YET: dict[type, TurnEvent] = {
    CallStarted: CallStarted(run_id=RUN, message_id=MESSAGE, call_id="toolu_01", name="x__y"),
    ArgumentsDelta: ArgumentsDelta(run_id=RUN, message_id=MESSAGE, call_id="toolu_01", text="{}"),
    CallCompleted: CallCompleted(run_id=RUN, message_id=MESSAGE, call_id="toolu_01"),
    ResultLanded: ResultLanded(run_id=RUN, message_id=MESSAGE, call_id="toolu_01", text="ok"),
}
"""The tool events a run publishes, which the wire maps in the step that follows
(``docs/working-notes/mcp-plan.md``, step 6): until then the mapper refuses them
loudly, as a mistake of ours, rather than sending something it invented."""


def test_every_kind_of_turn_event_is_mapped_or_named_as_not_yet() -> None:
    """The platform's events are a closed set, and this is the whole of it."""
    one = mapper()
    sample: dict[type, TurnEvent] = {
        RunStarted: started(),
        MessageStarted: announced(),
        TextDelta: text("Someone"),
        ReasoningDelta: thought("Hmm."),
        MessageCompleted: completed(),
        RunEnded: RunEnded(run_id=RUN, state=RunState.FINISHED),
    }

    assert set(sample) | set(NOT_MAPPED_YET) == set(get_args(TurnEvent))
    for event in sample.values():
        assert one.of(RunEvent(run_id=RUN, seq=1, event=event)) != ()
    for event in NOT_MAPPED_YET.values():
        with pytest.raises(RobinautsError, match=UNMAPPED):
            mapper().of(RunEvent(run_id=RUN, seq=1, event=event))


def test_an_event_kind_nobody_mapped_is_a_mistake_of_ours() -> None:
    """Not a refusal of the request: a stream carries what the platform wrote."""

    class Invented:
        run_id = RUN

    unmapped = RunEvent(run_id=RUN, seq=1, event=started())
    object.__setattr__(unmapped, "event", Invented())

    with pytest.raises(RobinautsError) as raised:
        mapper().of(unmapped)

    assert UNMAPPED in str(raised.value)


# --- how one crosses the wire -------------------------------------------------


def test_an_event_is_sent_with_its_position_as_the_id() -> None:
    """``Last-Event-ID`` re-attaching is native because of this line."""
    one = mapper()
    (begun,) = one.of(RunEvent(run_id=RUN, seq=7, event=started()))

    written = sse(begun, position=7)

    assert written.startswith(f"id: 7\nevent: {EventType.RUN_STARTED.value}\ndata: ")
    assert written.endswith("\n\n")
    assert json.loads(written.split("data: ", 1)[1])["runId"] == str(RUN)


def test_an_event_the_platform_did_not_number_carries_no_id() -> None:
    """So a client that re-attaches after one asks from the last real position."""
    one = mapper()
    (ended,) = one.of(
        RunEvent(run_id=RUN, seq=1, event=RunEnded(run_id=RUN, state=RunState.FINISHED))
    )

    assert sse(ended).startswith(f"event: {EventType.RUN_FINISHED.value}\n")
    assert "id:" not in sse(ended)


def test_the_media_type_is_the_one_the_packages_encoder_names() -> None:
    assert EventEncoder().get_content_type() == SSE_MEDIA_TYPE
