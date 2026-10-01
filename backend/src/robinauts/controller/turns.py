# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The turn runner: the engine's stream, stored as numbered turn events and an answer."""

from __future__ import annotations

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
    Conversation,
    Message,
    MessageCompleted,
    MessagePart,
    MessageStarted,
    ReasoningPiece,
    ResultLanded,
    Role,
    TextPart,
    TextPiece,
    ToolCallPart,
    ToolResultPart,
    TurnEnded,
    TurnState,
)
from robinauts.controller.store import Store


async def run_turn(
    store: Store,
    engine: AgentEngine,
    conversation: Conversation,
    question: Message,
    agent_config: AgentConfig,
    model: str,
    checkpoint_id: str | None,
) -> None:
    cid = conversation.id
    answer_id = uuid.uuid4()
    prompt = "".join(p.text for p in question.parts if isinstance(p, TextPart))
    definition = AgentDefinition(agent_config.system_prompt, agent_config.tools)
    parts: list[MessagePart] = []
    try:
        await store.append_event(cid, MessageStarted(answer_id, parent_id=question.id))
        async for event in engine.stream(
            cid, definition, prompt, model=model, checkpoint_id=checkpoint_id, timeout_seconds=120.0
        ):
            if isinstance(event, TextDelta):
                await store.append_event(cid, TextPiece(answer_id, event.text))
            elif isinstance(event, ReasoningDelta):
                await store.append_event(cid, ReasoningPiece(answer_id, event.text))
            elif isinstance(event, ToolCall):
                await store.append_event(cid, CallStarted(answer_id, event.call_id, event.name))
                arguments = json.dumps(dict(event.arguments))
                await store.append_event(cid, ArgumentsPiece(answer_id, event.call_id, arguments))
                await store.append_event(cid, CallCompleted(answer_id, event.call_id))
                parts.append(ToolCallPart(event.call_id, event.name, event.arguments))
            elif isinstance(event, ToolResult):
                await store.append_event(
                    cid, ResultLanded(answer_id, event.call_id, event.output, event.is_error)
                )
                parts.append(ToolResultPart(event.call_id, event.output, event.is_error))
            elif isinstance(event, Done):
                answer = Message(
                    answer_id,
                    cid,
                    parent_id=question.id,
                    role=Role.ASSISTANT,
                    parts=(*parts, TextPart(event.text)),
                    created_at=datetime.now(UTC),
                    agent=conversation.agent,
                    model=model,
                    checkpoint_id=event.checkpoint_id,
                )
                await store.add_message(answer)
                await store.append_event(cid, MessageCompleted(answer))
                await store.append_event(cid, TurnEnded(TurnState.FINISHED))
    except Exception as exc:
        await store.append_event(cid, TurnEnded(TurnState.FAILED, error=str(exc)))
    await store.update_conversation(dataclasses.replace(conversation, updated_at=datetime.now(UTC)))
    await store.end_turn(cid)
