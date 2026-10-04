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


class SessionNotFoundError(ControllerError):
    """Also for a session that exists and is not the user's."""


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


class ConfigError(ControllerError):
    """A configuration that cannot be used; the message lists every problem found."""


class TurnActiveError(ControllerError):
    """A turn asked for while the session already has one running."""


class TurnLostError(ControllerError):
    """A runner's write refused: the turn is no longer running, its lease has passed, or
    another runner holds the position. The runner writes nothing more."""


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
    """One call to the vendor, never the turn: the turn's is ``WorkConfig.max_turn_seconds``."""
    max_output_tokens: int | None = None
    context_window: int | None = None
    title: str = ""
    max_retries: int = 2
    """How many times the vendor's client tries a call again, with backoff, on a rate limit,
    an overload or a dropped connection."""


class ToolErrors(StrEnum):
    """What a tool's error does to the turn."""

    REPORT = "report"
    """The error is the call's result: the model reads it and the turn goes on."""
    RETRY = "retry"
    """Pydantic AI asks the model to correct the call, within its retries, and then fails the
    turn. The LangGraph engine always reports."""


class ToolServerAuth(StrEnum):
    BEARER = "bearer"
    BASIC = "basic"
    HEADER = "header"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class ToolServerConfig:
    id: str
    url: str
    secret_env: str = ""
    auth: ToolServerAuth = ToolServerAuth.BEARER
    user: str = ""
    timeout_seconds: float = 60.0
    # The header `auth = "header"` sends the secret in, as it is; set for that mode alone.
    header: str = ""
    tool_errors: ToolErrors = ToolErrors.REPORT


@dataclass(frozen=True, slots=True)
class WorkConfig:
    """How turns run, the ``[work]`` table: the same in every replica."""

    max_turn_seconds: float = 1200.0
    """A turn's deadline, from its start: ``turns.deadline_at``."""
    max_model_calls: int = 100
    """The model calls one turn may make, its tool loop included."""
    lease_seconds: float = 90.0
    """How long a turn stays its runner's without a heartbeat: a dead pod's turns are found
    this long after its last one."""
    heartbeat_seconds: float = 30.0
    """How often a pod renews the leases of the turns it runs, in one write for all of them."""


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
    work: WorkConfig = field(default_factory=WorkConfig)


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


# --- credentials --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UserSession:
    """A signed-in browser. Its cookie holds the secret, and this the secret's SHA-256 hex."""

    id: uuid.UUID
    user_id: uuid.UUID
    secret_hash: str
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class PendingLogin:
    """A sign-in begun and not yet finished, under the SHA-256 hex of its ``state``. Its nonce
    and PKCE verifier are kept as they are, since the callback needs them."""

    state_hash: str
    provider: str
    nonce: str
    verifier: str
    return_to: str
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ApiToken:
    """Its owner was shown the secret once; this holds the secret's SHA-256 hex."""

    id: uuid.UUID
    user_id: uuid.UUID
    name: str
    secret_hash: str
    created_at: datetime
    expires_at: datetime


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


# --- sessions -----------------------------------------------------------------


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
    session_id: uuid.UUID
    parent_id: uuid.UUID | None
    role: Role
    parts: tuple[MessagePart, ...]
    created_at: datetime
    agent: str | None = None
    engine: str | None = None
    model: str | None = None
    checkpoint_id: str | None = None
    turn_id: uuid.UUID | None = None
    """The turn that produced an answer, which web shows as the run id; ``None`` on a question."""
    failed: bool = False
    """An answer whose turn failed: what it streamed before it failed, and no checkpoint."""


@dataclass(frozen=True, slots=True)
class Session:
    id: uuid.UUID
    owner_id: uuid.UUID
    agent: str
    engine: str
    """The engine that holds the session's memory: the agent's when the session was made."""

    created_at: datetime
    updated_at: datetime
    title: str = ""


@dataclass(frozen=True, slots=True)
class SessionPage:
    sessions: tuple[Session, ...]
    cursor: str | None = None


class TurnState(StrEnum):
    RUNNING = "running"
    FINISHED = "finished"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class Turn:
    """One question, and everything done to answer it. Its id is the run id on the wire."""

    id: uuid.UUID
    session_id: uuid.UUID
    follows: uuid.UUID
    model: str
    state: TurnState
    started_at: datetime
    lease_until: datetime
    ended_at: datetime | None = None
    error: str | None = None
    """For the operator, on a turn that ended badly; never sent to a browser."""
    retries: uuid.UUID | None = None
    """The failed answer this turn tries again, which the model is told about."""
    deadline_at: datetime | None = None
    """When the turn must have ended: its start plus ``WorkConfig.max_turn_seconds``."""
    worker_id: str | None = None
    """The pod that runs the turn, or ran it last."""
    attempt: int = 0
    """Which run of the turn holds it: every write of its runner names it."""
    heartbeat_at: datetime | None = None
    """When its runner last renewed the lease."""
    cancel_requested_at: datetime | None = None
    """When a cancel was asked for, from any pod; the runner's pod acts on it."""


@dataclass(frozen=True, slots=True)
class ActiveTurn:
    """The turn a session is in the middle of: which, what it answers, and where its events are."""

    turn_id: uuid.UUID
    follows: uuid.UUID
    position: int


@dataclass(frozen=True, slots=True)
class OpenedSession:
    """One moment of a session: its record, the thread it shows, oldest first, and its turn."""

    session: Session
    messages: tuple[Message, ...]
    active: ActiveTurn | None = None
    ended_badly: Turn | None = None
    """The session's last turn, when it failed, was cancelled or was interrupted."""


# --- turns --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TurnStarted:
    session_id: uuid.UUID
    turn_id: uuid.UUID
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
    """The answer is stored in the same operation; a copy here would store it twice."""

    message_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class TurnEnded:
    """The state alone: the error is on the turn's record, for the operator."""

    state: TurnState


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
