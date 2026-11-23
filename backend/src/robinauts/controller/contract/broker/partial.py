# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What a turn had answered when its runner went away, rebuilt from the events it stored."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime

from robinauts.controller.contract.broker.documents import event_from_document
from robinauts.controller.contract.broker.domain import Document
from robinauts.shared_domain.domain import (
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
)


def with_text(parts: list[MessagePart], text: str) -> None:
    """Text that arrives in a row is one part, until a tool call comes between."""
    if parts and isinstance(parts[-1], TextPart):
        parts[-1] = TextPart(parts[-1].text + text)
    else:
        parts.append(TextPart(text))


def failed_answer(
    answer_id: uuid.UUID, session: Session, turn: Turn, parts: Sequence[MessagePart], at: datetime
) -> Message:
    """What a turn that did not finish keeps: what it had built, as an answer marked failed,
    under its question. The thread shows it, and a reply hangs under it."""
    return Message(
        answer_id,
        session.id,
        parent_id=turn.follows,
        role=Role.ASSISTANT,
        parts=tuple(parts),
        created_at=at,
        agent=session.agent,
        engine=session.engine,
        model=turn.model,
        turn_id=turn.id,
        failed=True,
    )


def partial_answer(events: Iterable[Document]) -> tuple[uuid.UUID | None, tuple[MessagePart, ...]]:
    """The answer's id, ``None`` before it started, and the parts the runner had built: its
    text, and each tool call that completed and each result. A call cut short is left out."""
    answer_id = None
    parts: list[MessagePart] = []
    calls: dict[str, tuple[str, str]] = {}
    for document in events:
        event = event_from_document(document).event
        if isinstance(event, MessageStarted):
            answer_id = event.message_id
        elif isinstance(event, TextPiece):
            with_text(parts, event.text)
        elif isinstance(event, CallStarted):
            calls[event.call_id] = (event.name, "")
        elif isinstance(event, ArgumentsPiece) and event.call_id in calls:
            name, arguments = calls[event.call_id]
            calls[event.call_id] = (name, arguments + event.text)
        elif isinstance(event, CallCompleted) and event.call_id in calls:
            name, arguments = calls.pop(event.call_id)
            parts.append(ToolCallPart(event.call_id, name, json.loads(arguments or "{}")))
        elif isinstance(event, ResultLanded):
            parts.append(
                ToolResultPart(event.call_id, event.text, event.is_error, event.structured)
            )
    return answer_id, tuple(parts)
