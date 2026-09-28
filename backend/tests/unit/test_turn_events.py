# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What a running turn streams: the platform's own events, not the wire's."""

import random
import uuid

import pytest

from conversations import CONVERSATION, RUN, answer, question
from robinauts.domain import (
    ACTIVE_RUN_STATES,
    ENDED_RUN_STATES,
    FIRST_POSITION,
    MAX_EXTRAS_BYTES,
    MAX_PART_CHARS,
    MAX_POSITION,
    AnswerCompleted,
    AnswerReasoningDelta,
    AnswerStarted,
    AnswerTextDelta,
    ArgumentsDelta,
    CallCompleted,
    CallStarted,
    EngineEvent,
    InvalidValueError,
    MessageCompleted,
    MessageStarted,
    ReasoningDelta,
    ResultLanded,
    Role,
    RunEnded,
    RunEvent,
    RunStarted,
    RunState,
    TextDelta,
    TextPart,
    TurnEvent,
    clean_text,
    flush,
    publishable,
)

MESSAGE = uuid.UUID("55555555-5555-4555-8555-555555555555")
PARENT = uuid.UUID("66666666-6666-4666-8666-666666666666")


def started(**changes: object) -> MessageStarted:
    fields: dict[str, object] = {"run_id": RUN, "message_id": MESSAGE, "parent_id": PARENT}
    fields.update(changes)
    return MessageStarted(**fields)  # type: ignore[arg-type]


def test_a_stream_begins_with_the_run_and_names_the_conversation() -> None:
    begun = RunStarted(run_id=RUN, conversation_id=CONVERSATION)
    assert (begun.run_id, begun.conversation_id) == (RUN, CONVERSATION)
    assert isinstance(begun, TurnEvent)


def test_a_message_says_what_it_will_be_before_any_of_it_exists() -> None:
    announced = started()
    assert (announced.message_id, announced.parent_id, announced.role) == (
        MESSAGE,
        PARENT,
        Role.ASSISTANT,
    )


def test_a_message_a_run_produces_is_never_a_root() -> None:
    with pytest.raises(InvalidValueError):
        started(parent_id=None)


def test_a_message_is_announced_under_a_role_this_build_carries() -> None:
    assert started().role is Role.ASSISTANT
    # A tool message is announced too: it is the platform's, produced by a
    # run once the calls of its parent have been answered.
    assert started(role=Role.TOOL).role is Role.TOOL
    with pytest.raises(InvalidValueError, match="is a Role"):
        started(role="assistant")


def test_a_message_is_not_its_own_parent() -> None:
    with pytest.raises(InvalidValueError, match="its own parent"):
        started(parent_id=MESSAGE)


def test_a_run_never_announces_a_question() -> None:
    """A run answers one; the person wrote it, and it is stored before the
    run begins."""
    with pytest.raises(InvalidValueError, match="answers, not questions"):
        started(role=Role.USER)


def test_a_message_arrives_in_pieces() -> None:
    events = [
        started(),
        ReasoningDelta(run_id=RUN, message_id=MESSAGE, text="thinking"),
        TextDelta(run_id=RUN, message_id=MESSAGE, text="Hi"),
    ]
    assert all(isinstance(event, TurnEvent) for event in events)
    assert all(event.run_id == RUN for event in events)


def test_what_the_platform_publishes_is_storable_text() -> None:
    """These are written into the run's events and sent over the wire: a
    delta that could not be encoded could not be stored or sent."""
    for half in ("a\ud800", "\x00"):
        with pytest.raises(InvalidValueError):
            TextDelta(run_id=RUN, message_id=MESSAGE, text=half)
        with pytest.raises(InvalidValueError):
            ReasoningDelta(run_id=RUN, message_id=MESSAGE, text=half)


def test_a_half_of_a_character_waits_for_its_other_half() -> None:
    """What the application does between the engine's fragments and its own
    deltas: one function, because the order of what it does is the whole of
    its correctness."""
    carry = ""
    published = []
    for fragment in ("a\ud83d", "\x00", "\ude00b", "c"):
        pieces, carry = publishable(carry, fragment)
        published.extend(TextDelta(run_id=RUN, message_id=MESSAGE, text=piece) for piece in pieces)
    assert [delta.text for delta in published] == ["a", "\U0001f600b", "c"]
    assert flush(carry) == ""

    # A half that never finds its other half is replaced, at the end.
    pieces, carry = publishable("", "\ud83d")
    assert (pieces, carry) == ((), "\ud83d")
    assert TextDelta(run_id=RUN, message_id=MESSAGE, text=flush(carry)).text == "\ufffd"


def test_what_is_published_always_fits_in_one_delta() -> None:
    """A held-back half makes a fragment at the bound one character too long
    for a single delta, so it comes back as two pieces."""
    pieces, carry = publishable("\ud83d", "a" * MAX_PART_CHARS)
    assert [len(piece) for piece in pieces] == [MAX_PART_CHARS, 1]
    assert carry == ""
    assert "".join(pieces) == "\ufffd" + "a" * MAX_PART_CHARS
    for piece in pieces:
        assert TextDelta(run_id=RUN, message_id=MESSAGE, text=piece).text == piece


def test_everything_published_is_everything_that_is_stored() -> None:
    """The promise, over generated fragments: what a person watched arrive
    is what is stored, character for character -- and a NUL between the two
    halves of a character does not take them apart."""
    rng = random.Random(20260921)
    alphabet = [
        "a",
        "\u4e2d",
        "\x00",
        "\ud83d",
        "\ude00",
        "\ud800",
        "\udfff",
        "",
        "b\x00\ud83d",
        "\ude00\x00c",
    ]
    for _ in range(4_000):
        fragments = [rng.choice(alphabet) for _ in range(rng.randint(0, 8))]
        carry, published = "", []
        for fragment in fragments:
            pieces, carry = publishable(carry, fragment)
            for piece in pieces:
                assert piece, "nothing empty is published"
                assert len(piece) <= MAX_PART_CHARS
                assert TextDelta(run_id=RUN, message_id=MESSAGE, text=piece).text == piece
            published.extend(pieces)
        published.append(flush(carry))
        assert "".join(published) == clean_text("".join(fragments))


def test_a_position_counts_no_further_than_a_whole_number_travels() -> None:
    """JSON has one number type and a browser reads it as a double."""
    begun = RunStarted(run_id=RUN, conversation_id=CONVERSATION)
    assert RunEvent(run_id=RUN, seq=MAX_POSITION, event=begun).seq == MAX_POSITION
    with pytest.raises(InvalidValueError, match="between"):
        RunEvent(run_id=RUN, seq=MAX_POSITION + 1, event=begun)


def test_a_completed_message_carries_the_message_as_it_was_stored() -> None:
    message = answer(question())
    assert MessageCompleted(run_id=RUN, message=message).message == message
    with pytest.raises(InvalidValueError, match="is a Message"):
        MessageCompleted(run_id=RUN, message={"role": "assistant"})


@pytest.mark.parametrize("state", sorted(ENDED_RUN_STATES))
def test_a_stream_ends_in_the_state_the_run_ended_in(state: RunState) -> None:
    error = "the provider said no" if state is RunState.FAILED else None
    event = RunEnded(run_id=RUN, state=state, error=error)
    assert event.state is state
    assert isinstance(event, TurnEvent)


@pytest.mark.parametrize("state", sorted(ACTIVE_RUN_STATES))
def test_a_run_that_has_not_ended_does_not_end_a_stream(state: RunState) -> None:
    with pytest.raises(InvalidValueError, match="has not ended"):
        RunEnded(run_id=RUN, state=state)


def test_a_failure_says_what_went_wrong_and_a_good_end_has_nothing_to_say() -> None:
    with pytest.raises(InvalidValueError, match="what went wrong"):
        RunEnded(run_id=RUN, state=RunState.FAILED)
    with pytest.raises(InvalidValueError, match="has no error"):
        RunEnded(run_id=RUN, state=RunState.FINISHED, error="something")
    assert RunEnded(run_id=RUN, state=RunState.CANCELLED, error="stopped").error == "stopped"


@pytest.mark.parametrize(
    "event",
    [
        lambda bad: RunStarted(run_id=bad, conversation_id=CONVERSATION),
        lambda bad: RunStarted(run_id=RUN, conversation_id=bad),
        lambda bad: started(message_id=bad),
        lambda bad: started(parent_id=bad),
        lambda bad: TextDelta(run_id=bad, message_id=MESSAGE, text="hi"),
        lambda bad: ReasoningDelta(run_id=RUN, message_id=bad, text="hi"),
        lambda bad: MessageCompleted(run_id=bad, message=question()),
        lambda bad: RunEnded(run_id=bad, state=RunState.FINISHED),
    ],
)
@pytest.mark.parametrize("bad", [str(RUN), "not a uuid", None, 7])
def test_every_event_is_about_a_real_id(event: object, bad: object) -> None:
    with pytest.raises(InvalidValueError):
        event(bad)


def test_a_delta_is_bounded_text() -> None:
    assert TextDelta(run_id=RUN, message_id=MESSAGE, text="a" * MAX_PART_CHARS).text
    with pytest.raises(InvalidValueError):
        TextDelta(run_id=RUN, message_id=MESSAGE, text="a" * (MAX_PART_CHARS + 1))
    with pytest.raises(InvalidValueError):
        ReasoningDelta(run_id=RUN, message_id=MESSAGE, text=7)


def test_a_runs_state_is_a_state() -> None:
    with pytest.raises(InvalidValueError, match="is a RunState"):
        RunEnded(run_id=RUN, state="finished")


# --- where an event falls in its run ----------------------------------------


def test_an_event_of_a_run_has_a_position_to_re_attach_by() -> None:
    event = RunStarted(run_id=RUN, conversation_id=CONVERSATION)
    numbered = RunEvent(run_id=RUN, seq=FIRST_POSITION, event=event)
    assert (numbered.seq, numbered.event) == (1, event)


@pytest.mark.parametrize("seq", [0, -1, 1.0, True, "1", None])
def test_a_position_is_a_whole_number_from_the_first_one(seq: object) -> None:
    with pytest.raises(InvalidValueError):
        RunEvent(run_id=RUN, seq=seq, event=RunStarted(run_id=RUN, conversation_id=CONVERSATION))


def test_an_envelope_and_what_it_carries_name_one_run() -> None:
    other = uuid.uuid4()
    with pytest.raises(InvalidValueError, match="one run"):
        RunEvent(run_id=other, seq=1, event=RunStarted(run_id=RUN, conversation_id=CONVERSATION))


def test_an_envelope_carries_a_turn_event() -> None:
    with pytest.raises(InvalidValueError, match="turn event"):
        RunEvent(run_id=RUN, seq=1, event={"type": "run_started"})


# --- what an engine yields --------------------------------------------------


def test_an_engine_says_what_the_model_said_and_nothing_about_a_run() -> None:
    """No ids, no times, no provenance: an engine has none of them."""
    for event in (
        AnswerStarted(),
        AnswerTextDelta(text="Hi"),
        AnswerReasoningDelta(text="thinking"),
        AnswerCompleted(parts=(TextPart("Hi"),)),
    ):
        assert isinstance(event, EngineEvent)
        assert not isinstance(event, TurnEvent)
        assert not hasattr(event, "run_id")
        assert not hasattr(event, "message_id")


def test_an_engines_delta_may_carry_half_a_character() -> None:
    assert AnswerTextDelta(text="a\ud800").text == "a\ud800"
    assert AnswerReasoningDelta(text="\x00").text == "\x00"
    with pytest.raises(InvalidValueError):
        AnswerTextDelta(text="a" * (MAX_PART_CHARS + 1))
    with pytest.raises(InvalidValueError):
        AnswerTextDelta(text=7)


def test_a_completed_answer_carries_the_vendors_extras_as_a_bounded_copy() -> None:
    """What a vendor needs back with the history, and the platform never
    reads: kept as a copy, bounded as every ``extras`` is, and empty unless
    the engine said otherwise."""
    assert AnswerCompleted(parts=(TextPart("hi"),)).extras == {}
    given: dict[str, object] = {"anthropic": {"thinking": [{"signature": "sig"}]}}
    completed = AnswerCompleted(parts=(TextPart("hi"),), extras=given)
    given["anthropic"] = "changed"
    assert completed.extras == {"anthropic": {"thinking": [{"signature": "sig"}]}}
    with pytest.raises(InvalidValueError, match="at most"):
        AnswerCompleted(parts=(TextPart("hi"),), extras={"v": "x" * MAX_EXTRAS_BYTES})
    for broken in ("text", ["a"], {"a": object()}):
        with pytest.raises(InvalidValueError):
            AnswerCompleted(parts=(TextPart("hi"),), extras=broken)  # type: ignore[arg-type]


def test_a_completed_answer_is_the_platforms_own_content() -> None:
    assert AnswerCompleted(parts=[TextPart("one")]).parts == (TextPart("one"),)
    with pytest.raises(InvalidValueError, match="at least one part"):
        AnswerCompleted(parts=())
    with pytest.raises(InvalidValueError):
        AnswerCompleted(parts=("one",))
    with pytest.raises(InvalidValueError):
        AnswerCompleted(parts=(TextPart("a\x00b"),))


def test_the_two_vocabularies_do_not_overlap() -> None:
    assert not isinstance(RunStarted(run_id=RUN, conversation_id=CONVERSATION), EngineEvent)
    assert not isinstance(started(), EngineEvent)
    assert not isinstance(AnswerStarted(), TurnEvent)


# --- the tool events a run publishes ----------------------------------------------


def test_a_call_is_announced_inside_a_message_with_the_vendors_id_and_the_full_name() -> None:
    made = CallStarted(run_id=RUN, message_id=MESSAGE, call_id="toolu_01", name="github__search")

    assert (made.call_id, made.name) == ("toolu_01", "github__search")
    with pytest.raises(InvalidValueError):
        CallStarted(run_id=RUN, message_id=MESSAGE, call_id="", name="github__search")
    with pytest.raises(InvalidValueError):
        CallStarted(run_id=RUN, message_id=MESSAGE, call_id="toolu_01", name="has space")
    with pytest.raises(InvalidValueError):
        CallStarted(run_id="run", message_id=MESSAGE, call_id="toolu_01", name="x")  # type: ignore[arg-type]


def test_arguments_and_results_are_storable_text_bounded_as_a_part_is() -> None:
    assert (
        ArgumentsDelta(run_id=RUN, message_id=MESSAGE, call_id="c", text='{"q": ').text == '{"q": '
    )
    with pytest.raises(InvalidValueError):
        ArgumentsDelta(run_id=RUN, message_id=MESSAGE, call_id="c", text="x" * (MAX_PART_CHARS + 1))
    with pytest.raises(InvalidValueError):
        ArgumentsDelta(run_id=RUN, message_id=MESSAGE, call_id="c", text="half \ud83d")
    landed = ResultLanded(run_id=RUN, message_id=MESSAGE, call_id="c", text="found")
    assert landed.is_error is False
    with pytest.raises(InvalidValueError, match="yes or no"):
        ResultLanded(run_id=RUN, message_id=MESSAGE, call_id="c", text="x", is_error="no")  # type: ignore[arg-type]
    with pytest.raises(InvalidValueError):
        ResultLanded(run_id=RUN, message_id=MESSAGE, call_id="c", text="x" * (MAX_PART_CHARS + 1))


def test_a_call_completes_by_id_alone() -> None:
    assert CallCompleted(run_id=RUN, message_id=MESSAGE, call_id="toolu_01").call_id == "toolu_01"
    with pytest.raises(InvalidValueError):
        CallCompleted(run_id=RUN, message_id=MESSAGE, call_id="two words")
