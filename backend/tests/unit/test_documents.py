# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The one encoding of a message and of a turn's event (``docs/architecture/data-model.md``)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest

from robinauts.controller.application.documents import (
    VERSION,
    clean_text,
    event_from_document,
    event_to_document,
    message_from_document,
    message_to_document,
)
from robinauts.controller.contract.domain import (
    ArgumentsPiece,
    CallCompleted,
    CallStarted,
    InvalidValueError,
    Message,
    MessageCompleted,
    MessageStarted,
    NumberedEvent,
    ReasoningPart,
    ReasoningPiece,
    ResultLanded,
    Role,
    TextPart,
    TextPiece,
    ToolCallPart,
    ToolResultPart,
    TurnEnded,
    TurnStarted,
    TurnState,
)

NOW = datetime(2026, 10, 2, 12, 0, 3, 141592, tzinfo=UTC)
SESSION = uuid.UUID("0b7e0000-0000-4000-8000-000000000001")
QUESTION = uuid.UUID("a3d20000-0000-4000-8000-000000000002")
ANSWER = uuid.UUID("6f1c0000-0000-4000-8000-000000000003")
TURN = uuid.UUID("c9e40000-0000-4000-8000-000000000004")


def answer() -> Message:
    return Message(
        ANSWER,
        SESSION,
        parent_id=QUESTION,
        role=Role.ASSISTANT,
        parts=(
            TextPart("Let me check the weather."),
            ReasoningPart("Lisbon, so Celsius."),
            ToolCallPart("toolu_01", "weather", {"city": "Lisbon", "days": [1, 2]}),
            ToolResultPart("toolu_01", "18°C, clear", is_error=False),
            TextPart("It is 18°C and clear in Lisbon."),
        ),
        created_at=NOW,
        agent="assistant",
        engine="langchain",
        model="sonnet",
        checkpoint_id="1f0a",
        turn_id=TURN,
    )


def test_a_message_round_trips_through_its_document() -> None:
    document = message_to_document(answer())
    assert list(document)[0] == "v"
    assert document["v"] == VERSION
    assert document["created_at"] == "2026-10-02T12:00:03.141592Z"
    assert document["id"] == str(ANSWER)
    assert document["turn_id"] == str(TURN)
    assert [p["kind"] for p in document["parts"]] == [
        "text",
        "reasoning",
        "tool_call",
        "tool_result",
        "text",
    ]
    assert json.loads(json.dumps(document)) == document
    assert message_from_document(document) == answer()


def test_a_question_has_every_field_with_nulls_where_nothing_applies() -> None:
    question = Message(
        QUESTION, SESSION, parent_id=None, role=Role.USER, parts=(TextPart("hi"),), created_at=NOW
    )
    document = message_to_document(question)
    assert document["parent_id"] is None
    assert document["agent"] is None
    assert document["turn_id"] is None
    assert message_from_document(document) == question


def test_a_time_in_another_zone_is_written_in_utc_and_one_without_a_zone_refused() -> None:
    lisbon = timezone(timedelta(hours=1))
    message = Message(QUESTION, SESSION, None, Role.USER, (), NOW.astimezone(lisbon))
    assert message_to_document(message)["created_at"] == "2026-10-02T12:00:03.141592Z"
    naive = Message(QUESTION, SESSION, None, Role.USER, (), NOW.replace(tzinfo=None))
    with pytest.raises(InvalidValueError, match="without its zone"):
        message_to_document(naive)


@pytest.mark.parametrize(
    ("event", "kind"),
    [
        (MessageStarted(ANSWER, QUESTION), "message_started"),
        (TextPiece(ANSWER, "It is"), "text_piece"),
        (ReasoningPiece(ANSWER, "Lisbon"), "reasoning_piece"),
        (CallStarted(ANSWER, "toolu_01", "weather"), "call_started"),
        (ArgumentsPiece(ANSWER, "toolu_01", '{"city":'), "arguments_piece"),
        (CallCompleted(ANSWER, "toolu_01"), "call_completed"),
        (ResultLanded(ANSWER, "toolu_01", "18°C", True), "result_landed"),
        (MessageCompleted(ANSWER), "message_completed"),
        (TurnEnded(TurnState.FINISHED), "turn_ended"),
    ],
)
def test_every_event_kind_round_trips(event: object, kind: str) -> None:
    document = event_to_document(TURN, 7, event)  # type: ignore[arg-type]
    assert list(document)[:4] == ["v", "kind", "turn_id", "position"]
    assert document["kind"] == kind
    assert document["position"] == 7
    assert json.loads(json.dumps(document)) == document
    assert event_from_document(document) == NumberedEvent(7, event)  # type: ignore[arg-type]


def test_turn_started_has_no_document() -> None:
    with pytest.raises(InvalidValueError, match="TurnStarted"):
        event_to_document(TURN, 1, TurnStarted(SESSION, TURN, answer()))


def test_a_nul_is_dropped_and_a_lone_surrogate_replaced_wherever_text_goes() -> None:
    message = Message(
        QUESTION,
        SESSION,
        None,
        Role.USER,
        (
            TextPart("a\x00b\ud800c"),
            ToolCallPart("toolu_01", "we\x00ather", {"ci\x00ty": "Lis\udc00bon", "n": ["x\x00"]}),
            ToolResultPart("toolu_01", "😀 split pair joined"),
        ),
        NOW,
    )
    parts = message_to_document(message)["parts"]
    assert parts[0]["text"] == "ab�c"
    assert parts[1]["name"] == "weather"
    assert parts[1]["arguments"] == {"city": "Lis�bon", "n": ["x"]}
    assert parts[2]["text"] == "\U0001f600 split pair joined"
    assert event_to_document(TURN, 1, TextPiece(ANSWER, "\x00\udfff"))["text"] == "�"
    assert clean_text("plain") == "plain"


def test_a_number_json_cannot_write_is_refused() -> None:
    message = Message(
        QUESTION, SESSION, None, Role.USER, (ToolCallPart("c", "n", {"x": float("nan")}),), NOW
    )
    with pytest.raises(InvalidValueError, match="JSON cannot write"):
        message_to_document(message)


@pytest.mark.parametrize(
    ("change", "said"),
    [
        (lambda d: d.update(extra=1), "a key nobody wrote: extra"),
        (lambda d: d.pop("v"), "has no version"),
        (lambda d: d.update(v=2), "version 2, above this build's 1"),
        (lambda d: d["parts"].append({"kind": "picture", "url": "x"}), "does not know: 'picture'"),
        (lambda d: d.update(created_at="2026-10-02T12:00:03Z"), "created_at is not a time"),
        (lambda d: d.update(id=str(ANSWER).upper()), "id is not an id"),
        (lambda d: d.update(role="system"), "role is not a role"),
        (lambda d: d["parts"][0].pop("text"), "lacks a key: text"),
    ],
)
def test_a_message_document_is_refused_by_name(change: object, said: str) -> None:
    document = message_to_document(answer())
    change(document)  # type: ignore[operator]
    with pytest.raises(InvalidValueError, match=said):
        message_from_document(document)


@pytest.mark.parametrize(
    ("change", "said"),
    [
        (lambda d: d.update(kind="turn_begun"), "does not know: 'turn_begun'"),
        (lambda d: d.update(v=2), "version 2, above this build's 1"),
        (lambda d: d.update(position=0), "position is not a position"),
        (lambda d: d.update(turn_id="c9e4"), "turn_id is not an id"),
        (lambda d: d.update(text=None), "text is not text"),
    ],
)
def test_an_event_document_is_refused_by_name(change: object, said: str) -> None:
    document = event_to_document(TURN, 3, TextPiece(ANSWER, "hi"))
    change(document)  # type: ignore[operator]
    with pytest.raises(InvalidValueError, match=said):
        event_from_document(document)
