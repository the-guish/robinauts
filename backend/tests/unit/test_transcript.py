# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""An answer's parts from its turn's events: what a turn left behind is shown as it was."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from robinauts.controller.contract.domain import (
    ArgumentsPiece,
    CallCompleted,
    CallStarted,
    MessageStarted,
    ReasoningPiece,
    ResultLanded,
    Role,
    Session,
    TextPart,
    TextPiece,
    ToolCallPart,
    ToolResultPart,
    Turn,
    TurnState,
)
from robinauts.controller.core.transcript import left_answer, parts_of

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
ANSWER = uuid.uuid4()
QUESTION = uuid.uuid4()


def test_text_in_a_row_is_one_part_and_a_call_comes_between() -> None:
    events = [
        MessageStarted(ANSWER, QUESTION),
        TextPiece(ANSWER, "Let me "),
        ReasoningPiece(ANSWER, "hmm"),
        TextPiece(ANSWER, "look. "),
        CallStarted(ANSWER, "c1", "add"),
        ArgumentsPiece(ANSWER, "c1", '{"a": '),
        ArgumentsPiece(ANSWER, "c1", "2}"),
        CallCompleted(ANSWER, "c1"),
        ResultLanded(ANSWER, "c1", "2", False),
        TextPiece(ANSWER, "Two."),
    ]
    assert parts_of(events) == (
        TextPart("Let me look. "),
        ToolCallPart("c1", "add", {"a": 2}),
        ToolResultPart("c1", "2", False),
        TextPart("Two."),
    )


def test_a_call_whose_arguments_never_completed_is_left_out() -> None:
    events = [
        CallStarted(ANSWER, "c1", "add"),
        ArgumentsPiece(ANSWER, "c1", '{"a": '),
        CallStarted(ANSWER, "c2", "echo"),
        CallCompleted(ANSWER, "c2"),
    ]
    assert parts_of(events) == (ToolCallPart("c2", "echo", {}),)


def test_what_a_turn_left_is_an_answer_marked_failed_under_its_question() -> None:
    session = Session(uuid.uuid4(), uuid.uuid4(), "a", "echo", NOW, NOW)
    turn = Turn(uuid.uuid4(), session.id, QUESTION, "m", TurnState.RUNNING, NOW, NOW)
    left = left_answer(
        session, turn, [MessageStarted(ANSWER, QUESTION), TextPiece(ANSWER, "Hi")], NOW
    )
    assert left is not None
    assert (left.id, left.parent_id, left.role, left.failed) == (
        ANSWER,
        QUESTION,
        Role.ASSISTANT,
        True,
    )
    assert (left.parts, left.turn_id, left.model, left.agent) == (
        (TextPart("Hi"),),
        turn.id,
        "m",
        "a",
    )
    assert left_answer(session, turn, [], NOW) is None
