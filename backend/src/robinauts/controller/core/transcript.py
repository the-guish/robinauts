# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""An answer rebuilt from its turn's events: what a turn that ended without its runner did.

The runner keeps its answer's parts as it streams them, and stores them when the turn ends.
A turn whose runner went away, with its process, ends without it; its events are then the
only record of what it did, and the parts are rebuilt from them, as the runner would have
kept them: text in a row is one part, a call with its arguments, a result where it landed.
Reasoning is shown and never stored, so it is left out here too.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from robinauts.controller.contract.domain import (
    ArgumentsPiece,
    CallStarted,
    MessagePart,
    MessageStarted,
    ResultLanded,
    TextPart,
    TextPiece,
    ToolCallPart,
    ToolResultPart,
    TurnEvent,
)


@dataclass(frozen=True, slots=True)
class Partial:
    """The answer a turn had started: its id, the question it answers, and its parts."""

    message_id: uuid.UUID
    parent_id: uuid.UUID
    parts: tuple[MessagePart, ...]


def partial_answer(events: Iterable[TurnEvent]) -> Partial | None:
    """The answer the events describe, or ``None`` when no answer was started."""
    started: MessageStarted | None = None
    parts: list[MessagePart] = []
    calls: dict[str, int] = {}
    arguments: dict[str, str] = {}
    for event in events:
        if isinstance(event, MessageStarted):
            started = event
        elif isinstance(event, TextPiece):
            if parts and isinstance(parts[-1], TextPart):
                parts[-1] = TextPart(parts[-1].text + event.text)
            else:
                parts.append(TextPart(event.text))
        elif isinstance(event, CallStarted):
            calls[event.call_id] = len(parts)
            parts.append(ToolCallPart(event.call_id, event.name, {}))
        elif isinstance(event, ArgumentsPiece):
            arguments[event.call_id] = arguments.get(event.call_id, "") + event.text
        elif isinstance(event, ResultLanded):
            parts.append(ToolResultPart(event.call_id, event.text, event.is_error))
    if started is None:
        return None
    for call_id, at in calls.items():
        call = parts[at]
        if isinstance(call, ToolCallPart):
            parts[at] = ToolCallPart(call_id, call.name, _arguments(arguments.get(call_id, "")))
    return Partial(started.message_id, started.parent_id, tuple(parts))


def _arguments(text: str) -> dict[str, object]:
    """A call's arguments as streamed; nothing, when they never arrived whole."""
    try:
        parsed = json.loads(text)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
