# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The canonical encoding of a run's events: the events table, and the wire."""

import uuid
from typing import Any

import pytest

from conversations import CONVERSATION, RUN, answer, question
from robinauts.core import (
    EXTRAS,
    event_from_data,
    event_to_data,
    run_event_from_data,
    run_event_from_stored,
    run_event_to_data,
)
from robinauts.core import conversation_format as format_module
from robinauts.domain import (
    FIRST_POSITION,
    FORMAT_VERSION,
    InvalidValueError,
    Message,
    MessageCompleted,
    MessageStarted,
    ReasoningDelta,
    Role,
    RunEnded,
    RunEvent,
    RunStarted,
    RunState,
    StoredDataError,
    TextDelta,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    TurnEvent,
    UnsupportedFormatError,
    clean_text,
)

MESSAGE = uuid.UUID("0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d")
PARENT = uuid.UUID("1b2c3d4e-5f6a-4b7c-8d9e-0f1a2b3c4d5e")


def every_event() -> list[TurnEvent]:
    """One of each, as a run produces them -- a turn with tools included."""
    said = answer(question(), "Some one")
    calling = answer(
        question(),
        parts=(TextPart("Let me look."), ToolCallPart("toolu_01", "github__search", {"q": "x"})),
        extras={"anthropic": {"thinking": [{"signature": "sig"}]}},
    )
    results = Message(
        id=uuid.uuid4(),
        conversation_id=CONVERSATION,
        parent_id=calling.id,
        role=Role.TOOL,
        parts=(ToolResultPart("toolu_01", "found", is_error=True),),
        created_at=calling.created_at,
    )
    return [
        RunStarted(run_id=RUN, conversation_id=CONVERSATION),
        MessageStarted(run_id=RUN, message_id=MESSAGE, parent_id=PARENT),
        MessageStarted(run_id=RUN, message_id=MESSAGE, parent_id=PARENT, role=Role.TOOL),
        TextDelta(run_id=RUN, message_id=MESSAGE, text="Some "),
        ReasoningDelta(run_id=RUN, message_id=MESSAGE, text="thinking"),
        MessageCompleted(run_id=RUN, message=said),
        MessageCompleted(run_id=RUN, message=calling),
        MessageCompleted(run_id=RUN, message=results),
        RunEnded(run_id=RUN, state=RunState.FINISHED),
        RunEnded(run_id=RUN, state=RunState.FAILED, error="the provider said no"),
        RunEnded(run_id=RUN, state=RunState.CANCELLED),
    ]


@pytest.mark.parametrize("event", every_event())
def test_every_event_survives_the_round_trip(event: TurnEvent) -> None:
    assert event_from_data(event_to_data(event)) == event


@pytest.mark.parametrize("event", every_event())
def test_every_event_survives_it_with_its_position(event: TurnEvent) -> None:
    numbered = RunEvent(run_id=RUN, seq=FIRST_POSITION + 6, event=event)
    data = run_event_to_data(numbered)
    assert data["format_version"] == FORMAT_VERSION
    assert data["seq"] == 7
    assert run_event_from_data(data) == numbered


def test_an_event_is_written_with_its_kind_in_front() -> None:
    assert event_to_data(RunStarted(run_id=RUN, conversation_id=CONVERSATION)) == {
        "kind": "run_started",
        "run_id": str(RUN),
        "conversation_id": str(CONVERSATION),
    }
    assert event_to_data(MessageStarted(run_id=RUN, message_id=MESSAGE, parent_id=PARENT)) == {
        "kind": "message_started",
        "run_id": str(RUN),
        "message_id": str(MESSAGE),
        "parent_id": str(PARENT),
        "role": "assistant",
    }
    assert event_to_data(RunEnded(run_id=RUN, state=RunState.CANCELLED))["error"] is None


def test_a_completed_message_carries_the_message_document() -> None:
    """One encoding of a message, wherever a message is written down."""
    said = answer(question(), "Some one")
    data = event_to_data(MessageCompleted(run_id=RUN, message=said))
    assert data["message"]["format_version"] == FORMAT_VERSION
    assert data["message"]["id"] == str(said.id)
    assert event_from_data(data).message == said


@pytest.mark.parametrize(
    "text",
    [
        "",
        "plain",
        "café 中文 \U0001f600",
        "a family \U0001f468‍\U0001f469‍\U0001f466",
        'quotes " and \\ and   and \t',
    ],
)
def test_a_delta_survives_whatever_text_it_carries(text: str) -> None:
    """What a provider sent, once the application made it storable."""
    delta = TextDelta(run_id=RUN, message_id=MESSAGE, text=clean_text(text))
    assert event_from_data(event_to_data(delta)) == delta


def test_a_document_is_plain_data_a_store_can_keep() -> None:
    import json

    for event in every_event():
        written = json.dumps(
            run_event_to_data(RunEvent(run_id=RUN, seq=1, event=event)), allow_nan=False
        )
        written.encode("utf-8")
        assert json.loads(written)["event"]["kind"]


# --- the same strictness as a message ---------------------------------------


def test_a_kind_this_format_has_no_name_for_is_refused() -> None:
    for kind in ("", "tool_call", "RUN_STARTED", 7, None):
        with pytest.raises(InvalidValueError):
            event_from_data({"kind": kind, "run_id": str(RUN)})


def test_a_key_that_is_not_there_is_refused() -> None:
    """Every document is written whole, an event's as much as a message's."""
    for event in every_event():
        whole = event_to_data(event)
        for key in whole:
            without = {name: value for name, value in whole.items() if name != key}
            with pytest.raises(InvalidValueError):
                event_from_data(without)
    numbered = run_event_to_data(RunEvent(run_id=RUN, seq=1, event=every_event()[0]))
    for key in ("format_version", "run_id", "seq", "event"):
        without = {name: value for name, value in numbered.items() if name != key}
        with pytest.raises((InvalidValueError, UnsupportedFormatError)):
            run_event_from_data(without)


def test_a_run_that_ended_always_writes_its_error_even_when_there_is_none() -> None:
    """Written whole means written whole: the key is there and it is null."""
    data = event_to_data(RunEnded(run_id=RUN, state=RunState.CANCELLED))
    assert data["error"] is None
    assert event_from_data(data) == RunEnded(run_id=RUN, state=RunState.CANCELLED)


def test_a_key_nobody_wrote_is_refused_by_count() -> None:
    data = event_to_data(RunStarted(run_id=RUN, conversation_id=CONVERSATION))
    with pytest.raises(InvalidValueError, match="1 key"):
        event_from_data({**data, "elapsed": 12})
    numbered = run_event_to_data(RunEvent(run_id=RUN, seq=1, event=every_event()[0]))
    with pytest.raises(InvalidValueError, match="1 key"):
        run_event_from_data({**numbered, "written_at": "now"})


def test_a_refusal_says_nothing_of_what_it_refused() -> None:
    secret = "a-password-somebody-typed-in-the-wrong-box"
    data = event_to_data(TextDelta(run_id=RUN, message_id=MESSAGE, text="Some"))
    for broken in ({"run_id": secret}, {"message_id": secret}, {"text": {"said": secret}}):
        with pytest.raises(InvalidValueError) as refused:
            event_from_data({**data, **broken})
        assert secret not in str(refused.value)
    with pytest.raises(InvalidValueError) as refused:
        event_from_data({**data, secret: 1})
    assert secret not in str(refused.value)


def test_an_event_reads_past_what_a_later_build_added() -> None:
    data = event_to_data(RunStarted(run_id=RUN, conversation_id=CONVERSATION))
    read = event_from_data({**data, EXTRAS: {"vendor": {"trace": "abc"}}})
    assert read == RunStarted(run_id=RUN, conversation_id=CONVERSATION)
    numbered = run_event_to_data(RunEvent(run_id=RUN, seq=1, event=read))
    assert run_event_from_data({**numbered, EXTRAS: {"written_by": "a later build"}})
    with pytest.raises(InvalidValueError):
        event_from_data({**data, EXTRAS: "not an object"})


@pytest.mark.parametrize(
    "changes",
    [
        {"run_id": "not a uuid"},
        {"run_id": None},
        {"conversation_id": str(MESSAGE).upper()},
        {"kind": "message_started"},
    ],
)
def test_a_field_of_the_wrong_kind_is_refused(changes: dict[str, object]) -> None:
    data = event_to_data(RunStarted(run_id=RUN, conversation_id=CONVERSATION))
    with pytest.raises(InvalidValueError):
        event_from_data({**data, **changes})


def test_a_run_ended_says_a_state_of_this_format_and_an_error_of_text() -> None:
    data = event_to_data(RunEnded(run_id=RUN, state=RunState.FAILED, error="no"))
    with pytest.raises(InvalidValueError):
        event_from_data({**data, "state": "stopped"})
    with pytest.raises(InvalidValueError):
        event_from_data({**data, "error": 7})
    with pytest.raises(InvalidValueError):
        event_from_data({**data, "state": "finished"})


def test_an_envelope_is_versioned_and_its_position_a_whole_number() -> None:
    data = run_event_to_data(RunEvent(run_id=RUN, seq=1, event=every_event()[0]))
    with pytest.raises(UnsupportedFormatError, match="reads up to"):
        run_event_from_data({**data, "format_version": FORMAT_VERSION + 1})
    with pytest.raises(UnsupportedFormatError, match="which version"):
        run_event_from_data({**data, "format_version": None})
    for seq in (True, 1.0, "1", None, 0):
        with pytest.raises(InvalidValueError):
            run_event_from_data({**data, "seq": seq})


def test_the_events_have_an_upgrade_table_of_their_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A message and an event are two documents; an upgrade for one handed
    the other would make nonsense of it."""

    def from_version_0(fields: dict[str, Any]) -> dict[str, Any]:
        lifted = dict(fields)
        lifted["event"] = lifted.pop("what")
        lifted["format_version"] = 1
        return lifted

    numbered = RunEvent(run_id=RUN, seq=1, event=every_event()[0])
    old = run_event_to_data(numbered)
    old["what"] = old.pop("event")
    old["format_version"] = 0

    with pytest.raises(UnsupportedFormatError, match="no upgrade from"):
        run_event_from_data(old)
    monkeypatch.setattr(format_module, "_EVENT_UPGRADES", {0: from_version_0})
    assert run_event_from_data(old) == numbered
    # And the message registry is untouched by the event one.
    assert format_module._MESSAGE_UPGRADES == {}


# --- reading our own rows ---------------------------------------------------


def test_a_stored_event_this_build_cannot_read_is_a_fault_of_ours() -> None:
    """The envelope is the document; a bare event is a piece of one and is
    never stored on its own, so there is no stored reader for it."""
    with pytest.raises(StoredDataError) as refused:
        run_event_from_stored({"kind": "whatever"})
    assert refused.value.__cause__ is not None
    numbered = RunEvent(run_id=RUN, seq=1, event=every_event()[0])
    assert run_event_from_stored(run_event_to_data(numbered)) == numbered


def test_only_the_formats_own_events_are_encoded() -> None:
    for value in ("run started", 7, None, {"kind": "run_started"}):
        with pytest.raises(InvalidValueError):
            event_to_data(value)
        with pytest.raises(InvalidValueError):
            run_event_to_data(value)
