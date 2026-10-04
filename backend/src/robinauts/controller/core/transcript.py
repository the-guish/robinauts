# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""An answer's parts, from the events its turn published.

The one rule for both the runner, which keeps what it streams, and whoever ends a turn its
runner left behind, which has the stored events and nothing else: text that arrives in a
row is one part until a tool call comes between; a call is a part once its arguments are
complete, with the arguments its pieces spell; a result is a part of its own. Reasoning is
shown and never kept. A call whose arguments never completed is left out, since there is no
call to show.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from robinauts.controller.contract.domain import (
    ArgumentsPiece,
    CallCompleted,
    CallStarted,
    Message,
    MessagePart,
    MessageStarted,
    ResultLanded,
    Role,
    Session,
    TextPart,
    TextPiece,
    ToolCallPart,
    ToolResultPart,
    Turn,
    TurnEvent,
)


def _arguments(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text) if text else {}
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def parts_of(events: Iterable[TurnEvent]) -> tuple[MessagePart, ...]:
    """The parts the events spell, in order."""
    parts: list[MessagePart] = []
    calls: dict[str, tuple[str, list[str]]] = {}
    for event in events:
        if isinstance(event, TextPiece):
            if parts and isinstance(parts[-1], TextPart):
                parts[-1] = TextPart(parts[-1].text + event.text)
            else:
                parts.append(TextPart(event.text))
        elif isinstance(event, CallStarted):
            calls[event.call_id] = (event.name, [])
        elif isinstance(event, ArgumentsPiece) and event.call_id in calls:
            calls[event.call_id][1].append(event.text)
        elif isinstance(event, CallCompleted) and event.call_id in calls:
            name, pieces = calls.pop(event.call_id)
            parts.append(ToolCallPart(event.call_id, name, _arguments("".join(pieces))))
        elif isinstance(event, ResultLanded):
            parts.append(ToolResultPart(event.call_id, event.text, event.is_error))
    return tuple(parts)


def answer_started(events: Iterable[TurnEvent]) -> MessageStarted | None:
    """The answer the events began, if they began one."""
    return next((e for e in events if isinstance(e, MessageStarted)), None)


def left_answer(
    session: Session, turn: Turn, events: Sequence[TurnEvent], at: datetime
) -> Message | None:
    """What a turn its runner left behind had answered, as an answer marked failed, which the
    thread shows and Retry starts over from; ``None`` when it never began one."""
    started = answer_started(events)
    if started is None:
        return None
    return Message(
        started.message_id,
        session.id,
        parent_id=started.parent_id,
        role=Role.ASSISTANT,
        parts=parts_of(events),
        created_at=at,
        agent=session.agent,
        engine=session.engine,
        model=turn.model,
        turn_id=turn.id,
        failed=True,
    )
