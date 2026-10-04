# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What a turn had answered, read back from its events.

The runner keeps the parts of its answer as it streams them, and stores them when the turn
ends. A turn whose runner went away stored nothing, and its events are all that is left of
what it did: these rebuild the same parts from them, so that whoever ends the turn stores
what the thread showed. Reasoning is streamed and never stored, as the runner's is not; a
call whose arguments never completed is left out, since what it was called with is not
known.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from robinauts.controller.contract.domain import (
    ArgumentsPiece,
    CallCompleted,
    CallStarted,
    MessagePart,
    MessageStarted,
    NumberedEvent,
    ResultLanded,
    Role,
    TextPart,
    TextPiece,
    ToolCallPart,
    ToolResultPart,
)


@dataclass(frozen=True, slots=True)
class PartialAnswer:
    """The answer a turn had begun: its id, the question it answers, and its parts so far."""

    message_id: uuid.UUID
    parent_id: uuid.UUID
    parts: tuple[MessagePart, ...]


def with_text(parts: list[MessagePart], text: str) -> None:
    """Text that arrives in a row is one part, until a tool call comes between."""
    if parts and isinstance(parts[-1], TextPart):
        parts[-1] = TextPart(parts[-1].text + text)
    else:
        parts.append(TextPart(text))


def partial_answer(events: Iterable[NumberedEvent]) -> PartialAnswer | None:
    """The answer the events began, with its parts in stream order; ``None`` when none was."""
    started: MessageStarted | None = None
    parts: list[MessagePart] = []
    calls: dict[str, tuple[str, list[str]]] = {}
    for numbered in sorted(events, key=lambda n: n.position):
        event = numbered.event
        if isinstance(event, MessageStarted) and event.role is Role.ASSISTANT:
            started = event
        elif isinstance(event, TextPiece):
            with_text(parts, event.text)
        elif isinstance(event, CallStarted):
            calls[event.call_id] = (event.name, [])
        elif isinstance(event, ArgumentsPiece) and event.call_id in calls:
            calls[event.call_id][1].append(event.text)
        elif isinstance(event, CallCompleted) and event.call_id in calls:
            name, pieces = calls.pop(event.call_id)
            try:
                arguments = json.loads("".join(pieces) or "{}")
            except ValueError:
                continue
            if isinstance(arguments, dict):
                parts.append(ToolCallPart(event.call_id, name, arguments))
        elif isinstance(event, ResultLanded):
            parts.append(ToolResultPart(event.call_id, event.text, event.is_error))
    if started is None:
        return None
    return PartialAnswer(started.message_id, started.parent_id, tuple(parts))
