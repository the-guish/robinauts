# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The turn runner: the engine's stream, stored as numbered turn events and an answer."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import uuid
from datetime import UTC, datetime

from robinauts.agent_engines.contract.domain import (
    AgentDefinition,
    Done,
    ReasoningDelta,
    TextDelta,
    ToolCall,
    ToolResult,
)
from robinauts.agent_engines.contract.ports import AgentEngine
from robinauts.controller.contract.domain import (
    AgentConfig,
    ArgumentsPiece,
    CallCompleted,
    CallStarted,
    Message,
    MessageCompleted,
    MessagePart,
    MessageStarted,
    ReasoningPiece,
    ResultLanded,
    Role,
    Session,
    TextPart,
    TextPiece,
    ToolCallPart,
    ToolResultPart,
    TurnEnded,
    TurnState,
)
from robinauts.controller.ports.store import Store


async def run_turn(
    store: Store,
    engine: AgentEngine,
    session: Session,
    question: Message,
    agent_config: AgentConfig,
    model: str,
    checkpoint_id: str | None,
) -> None:
    sid = session.id
    answer_id = uuid.uuid4()
    prompt = "".join(p.text for p in question.parts if isinstance(p, TextPart))
    definition = AgentDefinition(agent_config.system_prompt, agent_config.tools)
    parts: list[MessagePart] = []
    try:
        await store.append_event(sid, MessageStarted(answer_id, parent_id=question.id))
        async for event in engine.stream(
            sid, definition, prompt, model=model, checkpoint_id=checkpoint_id, timeout_seconds=120.0
        ):
            if isinstance(event, TextDelta):
                await store.append_event(sid, TextPiece(answer_id, event.text))
            elif isinstance(event, ReasoningDelta):
                await store.append_event(sid, ReasoningPiece(answer_id, event.text))
            elif isinstance(event, ToolCall):
                await store.append_event(sid, CallStarted(answer_id, event.call_id, event.name))
                arguments = json.dumps(dict(event.arguments))
                await store.append_event(sid, ArgumentsPiece(answer_id, event.call_id, arguments))
                await store.append_event(sid, CallCompleted(answer_id, event.call_id))
                parts.append(ToolCallPart(event.call_id, event.name, event.arguments))
            elif isinstance(event, ToolResult):
                await store.append_event(
                    sid, ResultLanded(answer_id, event.call_id, event.output, event.is_error)
                )
                parts.append(ToolResultPart(event.call_id, event.output, event.is_error))
            elif isinstance(event, Done):
                answer = Message(
                    answer_id,
                    sid,
                    parent_id=question.id,
                    role=Role.ASSISTANT,
                    parts=(*parts, TextPart(event.text)),
                    created_at=datetime.now(UTC),
                    agent=session.agent,
                    engine=session.engine,
                    model=model,
                    checkpoint_id=event.checkpoint_id,
                )
                await store.add_message(answer)
                await store.append_event(sid, MessageCompleted(answer_id))
                await store.append_event(sid, TurnEnded(TurnState.FINISHED))
    except asyncio.CancelledError:
        await store.append_event(sid, TurnEnded(TurnState.CANCELLED))
        raise
    except Exception:
        # The error goes on the turn's record once the store keeps turns.
        await store.append_event(sid, TurnEnded(TurnState.FAILED))
    finally:
        await store.update_session(dataclasses.replace(session, updated_at=datetime.now(UTC)))
        await store.end_turn(sid)
