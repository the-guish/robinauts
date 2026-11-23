# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The one encoding of a message and of a turn's event (``docs/architecture/data-model.md``)."""

from __future__ import annotations

import dataclasses
import json
import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest

from robinauts.controller.contract.broker.documents import (
    MAX_STRUCTURED_BYTES,
    VERSION,
    clean_text,
    event_from_document,
    event_to_document,
    kept_structured,
    message_from_document,
    message_to_document,
    result_in_event,
    result_in_message,
)
from robinauts.controller.contract.domain import InvalidValueError, NumberedEvent
from robinauts.shared_domain.domain import (
    ArgumentsPiece,
    CallCompleted,
    CallStarted,
    Message,
    MessageCompleted,
    MessageStarted,
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


@pytest.mark.parametrize(("failed", "written"), [(True, {"failed": True}), (False, {})])
def test_failed_is_written_only_when_set(failed: bool, written: dict[str, bool]) -> None:
    message = dataclasses.replace(answer(), checkpoint_id=None, failed=failed)
    document = message_to_document(message)
    assert {k: v for k, v in document.items() if k == "failed"} == written
    assert message_from_document(document) == message


def test_a_task_payload_is_written_only_when_set_and_round_trips() -> None:
    payload = {"task_key": "code_task/1", "repositories": {"app": {"branch": "develop"}}}
    asked = dataclasses.replace(answer(), task_payload=payload)
    document = message_to_document(asked)
    assert document["task_payload"] == payload
    assert message_from_document(document) == asked
    assert "task_payload" not in message_to_document(answer())


def test_a_result_s_structured_content_is_written_only_when_it_has_one() -> None:
    layout = {"nodes": [{"id": "a", "x": 0.5}], "revision": 2}
    drawn = dataclasses.replace(
        answer(), parts=(ToolResultPart("toolu_01", "Two boxes.", False, layout),)
    )
    document = message_to_document(drawn)
    assert document["parts"][0]["structured"] == layout
    assert json.loads(json.dumps(document)) == document
    assert message_from_document(document) == drawn
    assert all("structured" not in part for part in message_to_document(answer())["parts"])


@pytest.mark.parametrize(
    ("structured", "kept"),
    [
        (None, None),
        ({}, {}),
        ({"nodes": [1, 2.5, "a", None, True]}, {"nodes": [1, 2.5, "a", None, True]}),
        ({"na\x00me": "bo\x00x"}, {"name": "box"}),
        ({"x": "a" * MAX_STRUCTURED_BYTES}, None),
        # Measured as UTF-8: three bytes a character here, and six were it escaped to ASCII.
        ({"x": "図" * (MAX_STRUCTURED_BYTES // 4)}, {"x": "図" * (MAX_STRUCTURED_BYTES // 4)}),
        ({"x": "図" * (MAX_STRUCTURED_BYTES // 3)}, None),
        ({"x": float("nan")}, None),
        ({"x": {1, 2}}, None),
    ],
    ids=[
        "none",
        "empty",
        "json",
        "cleaned",
        "too-long",
        "not-ascii-and-short-enough",
        "not-ascii-and-too-long",
        "not-a-json-number",
        "not-json",
    ],
)
def test_the_structured_content_the_transcript_keeps(
    structured: dict[str, object] | None, kept: dict[str, object] | None
) -> None:
    assert kept_structured(structured) == kept


LANDED = ToolResultPart("toolu_01", "18°C", False, {"celsius": 18})


@pytest.mark.parametrize(
    ("message_id", "call_id", "found"),
    [
        (ANSWER, "toolu_01", ToolResultPart("toolu_01", "18°C, clear", is_error=False)),
        (ANSWER, "toolu_02", None),
        (QUESTION, "toolu_01", None),
    ],
    ids=["its-message-and-call", "another-call", "another-message"],
)
def test_a_call_s_result_is_found_in_its_message(
    message_id: uuid.UUID, call_id: str, found: ToolResultPart | None
) -> None:
    assert result_in_message(message_to_document(answer()), message_id, call_id) == found


@pytest.mark.parametrize(
    ("event", "message_id", "call_id", "found"),
    [
        (
            ResultLanded(ANSWER, "toolu_01", "18°C", False, {"celsius": 18}),
            ANSWER,
            "toolu_01",
            LANDED,
        ),
        (
            ResultLanded(ANSWER, "toolu_01", "18°C", False, {"celsius": 18}),
            ANSWER,
            "toolu_02",
            None,
        ),
        (
            ResultLanded(ANSWER, "toolu_01", "18°C", False, {"celsius": 18}),
            QUESTION,
            "toolu_01",
            None,
        ),
        (CallCompleted(ANSWER, "toolu_01"), ANSWER, "toolu_01", None),
    ],
    ids=["its-message-and-call", "another-call", "another-message", "another-kind"],
)
def test_a_call_s_result_is_found_in_the_event_it_landed_in(
    event: object, message_id: uuid.UUID, call_id: str, found: ToolResultPart | None
) -> None:
    document = event_to_document(TURN, 7, event)  # type: ignore[arg-type]
    assert result_in_event(document, message_id, call_id) == found


def test_a_search_for_a_result_decodes_no_other_document() -> None:
    # Neither is a document a reader accepts: a search passes over what it is not looking for.
    assert result_in_message({"id": str(QUESTION)}, ANSWER, "toolu_01") is None
    assert result_in_event({"kind": "text_piece"}, ANSWER, "toolu_01") is None


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
        (ResultLanded(ANSWER, "toolu_01", "18°C", False, {"celsius": 18}), "result_landed"),
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
    started = TurnStarted(SESSION, TURN, answer())
    with pytest.raises(InvalidValueError, match="TurnStarted"):
        event_to_document(TURN, 1, started)


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
    assert parts[0]["text"] == "ab\ufffdc"
    assert parts[1]["name"] == "weather"
    assert parts[1]["arguments"] == {"city": "Lis\ufffdbon", "n": ["x"]}
    assert parts[2]["text"] == "\U0001f600 split pair joined"
    assert event_to_document(TURN, 1, TextPiece(ANSWER, "\x00\udfff"))["text"] == "\ufffd"
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
        (lambda d: d.update(failed="yes"), "failed is not true or false"),
        (lambda d: d.update(task_payload=["x"]), "task_payload is not an object"),
        (lambda d: d.update(task_payload={"branch": "x"}), "task_payload has no task_key"),
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
