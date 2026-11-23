# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What keeps a turn within its model's window: Pydantic AI's history processor, our policy.

Tool results are the bulk of a long turn. One larger than a fifth of the window is cut. Past
70% of the window, every tool result but the last three is cleared at once. The history is
then the same up to its end until the next crossing, which is what lets the vendor's prompt
cache keep paying. The processed history is what the run keeps and saves.

A tool result's structured content is not the model's (``tools.py``): the engine reports it as
the tool answers, and the history then drops it. Left on the result it would be measured against
a window it takes no part of, and saved again with every checkpoint.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping

from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ToolReturnPart,
)

from robinauts.agent_engines.pydantic_ai_engine.tools import STRUCTURED_CONTENT

CLEAR_AT = 0.7
"""The share of the window past which older tool results are cleared."""

KEEP_RESULTS = 3
"""The newest tool results, which are never cleared."""

LARGEST_RESULT = 0.2
"""The share of the window one tool result may take; past it, the rest is cut."""

CHARS_PER_TOKEN = 4
"""The estimate used to measure a history: rendered, in characters, over this."""

CLEARED = "[An older tool result, cleared to keep the conversation within the model's window.]"


def within(window: int) -> Callable[[list[ModelMessage]], list[ModelMessage]]:
    """The history processor for a model whose window is ``window`` tokens."""
    largest = int(window * LARGEST_RESULT) * CHARS_PER_TOKEN

    def processor(messages: list[ModelMessage]) -> list[ModelMessage]:
        capped = [_with_returns(m, lambda part: _cut(_reported(part), largest)) for m in messages]
        if _tokens(capped) <= window * CLEAR_AT:
            return capped
        kept = {id(part) for part in _returns(capped)[-KEEP_RESULTS:]}
        return [_with_returns(m, lambda part: _cleared(part, kept)) for m in capped]

    return processor


def without_structured(messages: list[ModelMessage]) -> list[ModelMessage]:
    """The history without the structured content of its tool results: what a session saves."""
    return [_with_returns(m, _reported) for m in messages]


def _tokens(messages: list[ModelMessage]) -> float:
    return len(ModelMessagesTypeAdapter.dump_json(messages)) / CHARS_PER_TOKEN


def _returns(messages: list[ModelMessage]) -> list[ToolReturnPart]:
    return [
        part
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]


def _with_returns(
    message: ModelMessage, change: Callable[[ToolReturnPart], ToolReturnPart]
) -> ModelMessage:
    """The message with each tool result changed; itself when none changes. Never in place."""
    if not isinstance(message, ModelRequest):
        return message
    parts = [change(p) if isinstance(p, ToolReturnPart) else p for p in message.parts]
    if all(new is old for new, old in zip(parts, message.parts, strict=True)):
        return message
    return dataclasses.replace(message, parts=parts)


def _reported(part: ToolReturnPart) -> ToolReturnPart:
    """The result without the structured content ``TextToModel`` put on its metadata. The engine
    has reported it by the time a history is processed or saved."""
    metadata = part.metadata
    if not isinstance(metadata, Mapping) or STRUCTURED_CONTENT not in metadata:
        return part
    rest = {key: value for key, value in metadata.items() if key != STRUCTURED_CONTENT}
    return dataclasses.replace(part, metadata=rest or None)


def _cut(part: ToolReturnPart, largest: int) -> ToolReturnPart:
    text = part.model_response_str()
    if len(text) <= largest:
        return part
    note = f"\n[Cut: the result was {len(text)} characters.]"
    return dataclasses.replace(part, content=text[:largest] + note)


def _cleared(part: ToolReturnPart, kept: set[int]) -> ToolReturnPart:
    if id(part) in kept or part.content == CLEARED:
        return part
    return dataclasses.replace(part, content=CLEARED)
