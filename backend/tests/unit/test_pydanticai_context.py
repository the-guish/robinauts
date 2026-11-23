# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What the Pydantic AI engine's history keeps of a tool result: its text, cut or cleared as
the window asks, and none of the structured content a view draws."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from robinauts.agent_engines.pydantic_ai_engine.context import (
    CLEARED,
    within,
    without_structured,
)
from robinauts.agent_engines.pydantic_ai_engine.tools import STRUCTURED_CONTENT

WINDOW = 10_000
"""Tokens. The estimate is four characters to one, so the window is 40,000 characters."""

TEXT = "A diagram of two boxes."


def round_of(call: int, metadata: Any) -> list[ModelMessage]:
    """One question, one tool call, its result with ``metadata``, and an answer."""
    call_id = f"call_{call}"
    result = ToolReturnPart("show", TEXT, tool_call_id=call_id, metadata=metadata)
    return [
        ModelRequest(parts=[UserPromptPart(content="show it")]),
        ModelResponse(parts=[ToolCallPart("show", {}, tool_call_id=call_id)]),
        ModelRequest(parts=[result]),
        ModelResponse(parts=[TextPart(content="There it is.")]),
    ]


def results_of(messages: list[ModelMessage]) -> list[ToolReturnPart]:
    return [
        part
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]


@pytest.mark.parametrize(
    ("metadata", "kept"),
    [
        ({STRUCTURED_CONTENT: {"nodes": ["a", "b"]}}, None),
        ({STRUCTURED_CONTENT: {"nodes": []}, "other": 1}, {"other": 1}),
        ({"other": 1}, {"other": 1}),
        (None, None),
        ("not a mapping", "not a mapping"),
    ],
    ids=["structured-alone", "structured-and-more", "other-alone", "none", "not-a-mapping"],
)
def test_a_history_keeps_a_result_s_text_and_drops_its_structured_content(
    metadata: Any, kept: Any
) -> None:
    history = round_of(1, metadata)

    for processed in (without_structured(history), within(WINDOW)(history)):
        [result] = results_of(processed)
        assert (result.content, result.metadata) == (TEXT, kept)
    # Never in place: the history handed in is as it was.
    assert results_of(history)[0].metadata == metadata


def test_a_history_with_nothing_to_drop_is_handed_back_as_it_is() -> None:
    history = round_of(1, None)

    assert all(new is old for new, old in zip(without_structured(history), history, strict=True))


def test_structured_content_does_not_count_against_the_window() -> None:
    # Eight results whose text is a few words, with more structured content between them than
    # the whole window holds. The model reads none of it, so no result is cleared.
    large = {STRUCTURED_CONTENT: {"nodes": ["x" * 100] * 50}}
    history = [message for call in range(8) for message in round_of(call, large)]

    processed = within(WINDOW)(history)

    assert [result.content for result in results_of(processed)] == [TEXT] * 8
    # A window the text alone goes past still clears every result but the last three.
    small = within(WINDOW // 8)(history)
    assert [result.content for result in results_of(small)] == [CLEARED] * 5 + [TEXT] * 3
