# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What crosses the controller port, as plain data (``docs/architecture/controller.md``)."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

# --- errors -------------------------------------------------------------------


class ControllerError(Exception):
    pass


class InvalidValueError(ControllerError, ValueError):
    pass


class ConversationNotFoundError(ControllerError):
    """Also for a conversation that exists and is not the user's."""


class MessageNotFoundError(ControllerError):
    pass


class UnknownAgentError(ControllerError):
    pass


class UnknownModelError(ControllerError):
    pass


class UnknownEngineError(ControllerError):
    """An agent configured on an engine this build does not have."""


class UnreachableProviderError(ControllerError):
    """A model on a provider kind its agent's engine cannot reach."""


class MissingSecretError(ControllerError):
    """A key or a secret the environment does not hold."""


class TurnActiveError(ControllerError):
    """A turn asked for while the conversation already has one running."""


class NoActiveTurnError(ControllerError):
    pass


# --- configuration ------------------------------------------------------------


class ProviderKind(StrEnum):
    ANTHROPIC = "anthropic"
    ANTHROPIC_COMPATIBLE = "anthropic-compatible"
    OPENAI = "openai"
    OPENAI_COMPATIBLE = "openai-compatible"


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    id: str
    kind: ProviderKind
    api_key_env: str
    base_url: str | None = None


@dataclass(frozen=True, slots=True)
class ModelConfig:
    id: str
    provider: str
    name: str
    timeout_seconds: float = 120.0
    max_output_tokens: int | None = None
    context_window: int | None = None
    title: str = ""


class ToolServerAuth(StrEnum):
    BEARER = "bearer"
    BASIC = "basic"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class ToolServerConfig:
    id: str
    url: str
    secret_env: str = ""
    auth: ToolServerAuth = ToolServerAuth.BEARER
    user: str = ""
    timeout_seconds: float = 60.0


@dataclass(frozen=True, slots=True)
class AgentConfig:
    id: str
    title: str
    system_prompt: str
    model: str
    engine: str
    tools: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Config:
    providers: Mapping[str, ProviderConfig] = field(default_factory=dict)
    models: Mapping[str, ModelConfig] = field(default_factory=dict)
    tool_servers: Mapping[str, ToolServerConfig] = field(default_factory=dict)
    agents: Mapping[str, AgentConfig] = field(default_factory=dict)


class StorageKind(StrEnum):
    POSTGRES = "postgres"
    LOCAL = "local"
    IN_MEMORY = "in-memory"


@dataclass(frozen=True, slots=True)
class StorageConfig:
    """A database URL, a folder, or nothing, for a test or a local start."""

    kind: StorageKind
    url: str | None = None
    path: str | None = None


# --- users --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Identity:
    """Who a shell says is asking: a provider and a subject, as the provider names them."""

    provider: str
    subject: str
    name: str | None = None
    email: str | None = None


@dataclass(frozen=True, slots=True)
class User:
    id: uuid.UUID
    provider: str
    subject: str
    name: str | None = None
    email: str | None = None
    created_at: datetime | None = None


# --- catalogue ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AgentListing:
    id: str
    title: str
    default_model: str


@dataclass(frozen=True, slots=True)
class ModelListing:
    id: str
    title: str


# --- conversations ------------------------------------------------------------


class Role(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class TextPart:
    text: str


@dataclass(frozen=True, slots=True)
class ReasoningPart:
    text: str


@dataclass(frozen=True, slots=True)
class ToolCallPart:
    call_id: str
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolResultPart:
    call_id: str
    text: str
    is_error: bool = False


MessagePart = TextPart | ReasoningPart | ToolCallPart | ToolResultPart


@dataclass(frozen=True, slots=True)
class Message:
    id: uuid.UUID
    conversation_id: uuid.UUID
    parent_id: uuid.UUID | None
    role: Role
    parts: tuple[MessagePart, ...]
    created_at: datetime
    agent: str | None = None
    model: str | None = None
    checkpoint_id: str | None = None


@dataclass(frozen=True, slots=True)
class Conversation:
    id: uuid.UUID
    owner_id: uuid.UUID
    agent: str
    created_at: datetime
    updated_at: datetime
    title: str = ""


@dataclass(frozen=True, slots=True)
class ConversationPage:
    conversations: tuple[Conversation, ...]
    cursor: str | None = None


class TurnState(StrEnum):
    RUNNING = "running"
    FINISHED = "finished"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class ActiveTurn:
    """The turn a conversation is in the middle of: what it answers, and where its events are."""

    follows: uuid.UUID
    position: int


@dataclass(frozen=True, slots=True)
class OpenedConversation:
    """One moment of a conversation: its record, the thread it shows, oldest first, and its turn."""

    conversation: Conversation
    messages: tuple[Message, ...]
    active: ActiveTurn | None = None


# --- turns --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TurnStarted:
    conversation_id: uuid.UUID
    question: Message


@dataclass(frozen=True, slots=True)
class MessageStarted:
    message_id: uuid.UUID
    parent_id: uuid.UUID
    role: Role = Role.ASSISTANT


@dataclass(frozen=True, slots=True)
class TextPiece:
    message_id: uuid.UUID
    text: str


@dataclass(frozen=True, slots=True)
class ReasoningPiece:
    message_id: uuid.UUID
    text: str


@dataclass(frozen=True, slots=True)
class CallStarted:
    message_id: uuid.UUID
    call_id: str
    name: str


@dataclass(frozen=True, slots=True)
class ArgumentsPiece:
    message_id: uuid.UUID
    call_id: str
    text: str


@dataclass(frozen=True, slots=True)
class CallCompleted:
    message_id: uuid.UUID
    call_id: str


@dataclass(frozen=True, slots=True)
class ResultLanded:
    message_id: uuid.UUID
    call_id: str
    text: str
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class MessageCompleted:
    message: Message


@dataclass(frozen=True, slots=True)
class TurnEnded:
    state: TurnState
    error: str | None = None


TurnEvent = (
    TurnStarted
    | MessageStarted
    | TextPiece
    | ReasoningPiece
    | CallStarted
    | ArgumentsPiece
    | CallCompleted
    | ResultLanded
    | MessageCompleted
    | TurnEnded
)


@dataclass(frozen=True, slots=True)
class NumberedEvent:
    """A turn's event at its position, which is what a watcher asks to continue from."""

    position: int
    event: TurnEvent
