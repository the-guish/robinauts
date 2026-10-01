# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Builders for the conversation format, so a test says only what it is about.

Fixed ids and a fixed clock: a test that wants two conversations, a second
run or a particular order says so, and everything else is the same in every
test that reads them.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from robinauts.legacy.domain import (
    AgentDefinition,
    Channel,
    Conversation,
    Engine,
    Message,
    MessagePart,
    ModelConfig,
    Provenance,
    Role,
    Run,
    RunState,
    TextPart,
)

CONVERSATION = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER_CONVERSATION = uuid.UUID("22222222-2222-4222-8222-222222222222")
OWNER = uuid.UUID("33333333-3333-4333-8333-333333333333")
RUN = uuid.UUID("44444444-4444-4444-8444-444444444444")
AGENT = "assistant"
MODEL = "sonnet"
OTHER_MODEL = "opus"
"""A second model a test deployment offers, for a conversation moved off ``MODEL``."""

T0 = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)
"""When every test conversation begins."""


def at(seconds: float) -> datetime:
    """``seconds`` after the start; what orders the messages of a test."""
    return T0 + timedelta(seconds=seconds)


def agent_definition(**changes: object) -> AgentDefinition:
    """The agent every test that is not about agents runs."""
    fields: dict[str, object] = {
        "id": AGENT,
        "title": "Assistant",
        "system_prompt": "Play fair.",
        "model": MODEL,
        "engine": Engine.PYDANTIC_AI,
    }
    fields.update(changes)
    return AgentDefinition(**fields)  # type: ignore[arg-type]


def model_config(model_id: str = MODEL, **changes: object) -> ModelConfig:
    """A model a deployment offers, reached through a provider nobody calls."""
    fields: dict[str, object] = {"id": model_id, "provider": "anthropic", "name": model_id}
    fields.update(changes)
    return ModelConfig(**fields)  # type: ignore[arg-type]


def offered(*model_ids: str) -> dict[str, ModelConfig]:
    """The models a test deployment offers: ``MODEL``, which every agent here
    starts on, and any others the test names."""
    return {model_id: model_config(model_id) for model_id in (MODEL, *model_ids)}


def provenance(**changes: object) -> Provenance:
    fields: dict[str, object] = {
        "agent": AGENT,
        "engine": Engine.PYDANTIC_AI,
        "model": MODEL,
        "run_id": RUN,
    }
    fields.update(changes)
    return Provenance(**fields)  # type: ignore[arg-type]


def _parent_id(parent: Message | uuid.UUID | None) -> uuid.UUID | None:
    return parent.id if isinstance(parent, Message) else parent


def question(
    text: str = "What is a robinaut?",
    *,
    parent: Message | uuid.UUID | None = None,
    seconds: float = 0.0,
    conversation_id: uuid.UUID = CONVERSATION,
    parts: tuple[MessagePart, ...] | None = None,
    **changes: object,
) -> Message:
    """A message from a person."""
    fields: dict[str, object] = {
        "id": uuid.uuid4(),
        "conversation_id": conversation_id,
        "parent_id": _parent_id(parent),
        "role": Role.USER,
        "parts": parts if parts is not None else (TextPart(text),),
        "created_at": at(seconds),
        "channel": Channel.WEB,
    }
    fields.update(changes)
    return Message(**fields)  # type: ignore[arg-type]


def answer(
    parent: Message | uuid.UUID,
    text: str = "Someone who plays fair.",
    *,
    seconds: float = 1.0,
    conversation_id: uuid.UUID = CONVERSATION,
    parts: tuple[MessagePart, ...] | None = None,
    **changes: object,
) -> Message:
    """A message from an agent, with the provenance an answer records."""
    fields: dict[str, object] = {
        "id": uuid.uuid4(),
        "conversation_id": conversation_id,
        "parent_id": _parent_id(parent),
        "role": Role.ASSISTANT,
        "parts": parts if parts is not None else (TextPart(text),),
        "created_at": at(seconds),
        "channel": Channel.WEB,
        "provenance": provenance(),
    }
    fields.update(changes)
    return Message(**fields)  # type: ignore[arg-type]


def conversation(**changes: object) -> Conversation:
    fields: dict[str, object] = {
        "id": CONVERSATION,
        "owner_id": OWNER,
        "agent": AGENT,
        "model": MODEL,
        "created_at": at(0),
        "updated_at": at(0),
        "title": "What is a robinaut?",
    }
    fields.update(changes)
    return Conversation(**fields)  # type: ignore[arg-type]


def run(**changes: object) -> Run:
    fields: dict[str, object] = {
        "id": RUN,
        "conversation_id": CONVERSATION,
        "message_id": uuid.uuid4(),
        "agent": AGENT,
        "engine": Engine.PYDANTIC_AI,
        "model": MODEL,
        "state": RunState.RUNNING,
        "created_at": at(0),
        "started_at": at(0),
    }
    fields.update(changes)
    return Run(**fields)  # type: ignore[arg-type]


def ended(state: RunState, **changes: object) -> Run:
    """A run that ended in ``state``, with what that state asks for."""
    fields: dict[str, object] = {"state": state, "finished_at": at(2)}
    if state is RunState.FAILED:
        fields["error"] = "the provider said no"
    fields.update(changes)
    return run(**fields)
