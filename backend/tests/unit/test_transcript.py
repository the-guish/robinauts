# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A turn's answer, read back from its events, as the runner would have stored it."""

from __future__ import annotations

import uuid

from robinauts.controller.contract.domain import (
    ArgumentsPiece,
    CallCompleted,
    CallStarted,
    MessageStarted,
    NumberedEvent,
    ReasoningPiece,
    ResultLanded,
    TextPart,
    TextPiece,
    ToolCallPart,
    ToolResultPart,
    TurnEnded,
    TurnState,
)
from robinauts.controller.core.transcript import PartialAnswer, partial_answer

ANSWER = uuid.uuid4()
QUESTION = uuid.uuid4()


def numbered(*events: object) -> list[NumberedEvent]:
    return [NumberedEvent(n, e) for n, e in enumerate(events, start=1)]  # type: ignore[arg-type]


def test_the_parts_are_rebuilt_in_stream_order_as_the_runner_keeps_them() -> None:
    events = numbered(
        MessageStarted(ANSWER, QUESTION),
        TextPiece(ANSWER, "Let me "),
        ReasoningPiece(ANSWER, "thinking"),
        TextPiece(ANSWER, "look. "),
        CallStarted(ANSWER, "c1", "search"),
        ArgumentsPiece(ANSWER, "c1", '{"q": '),
        ArgumentsPiece(ANSWER, "c1", '"robins"}'),
        CallCompleted(ANSWER, "c1"),
        ResultLanded(ANSWER, "c1", "three hits", is_error=False),
        TextPiece(ANSWER, "Found "),
        TextPiece(ANSWER, "them."),
    )
    # Read in any order, they are put back in theirs.
    assert partial_answer(reversed(events)) == PartialAnswer(
        ANSWER,
        QUESTION,
        (
            TextPart("Let me look. "),
            ToolCallPart("c1", "search", {"q": "robins"}),
            ToolResultPart("c1", "three hits", False),
            TextPart("Found them."),
        ),
    )


def test_a_call_still_in_flight_is_kept_and_one_whose_arguments_never_completed_is_not() -> None:
    events = numbered(
        MessageStarted(ANSWER, QUESTION),
        CallStarted(ANSWER, "c1", "deploy"),
        ArgumentsPiece(ANSWER, "c1", "{}"),
        CallCompleted(ANSWER, "c1"),
        CallStarted(ANSWER, "c2", "deploy"),
        ArgumentsPiece(ANSWER, "c2", '{"to": "pro'),
    )
    answer = partial_answer(events)
    assert answer is not None
    assert answer.parts == (ToolCallPart("c1", "deploy", {}),)


def test_no_answer_was_begun_before_its_start() -> None:
    assert partial_answer([]) is None
    assert partial_answer(numbered(TurnEnded(TurnState.INTERRUPTED))) is None
    started = partial_answer(numbered(MessageStarted(ANSWER, QUESTION)))
    assert started == PartialAnswer(ANSWER, QUESTION, ())
