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


class DrainingError(ControllerError):
    """A turn asked of a process that is stopping: another one takes it."""


class BusyError(ControllerError):
    """The database had no connection free in time: try again."""


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
    """One call to the vendor, never the turn: ``WorkConfig.max_turn_seconds`` bounds that."""
    max_output_tokens: int | None = None
    context_window: int | None = None
    title: str = ""
    max_retries: int = 2
    """Retries of one call by the vendor's SDK, with its backoff."""


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


@dataclass(frozen=True, slots=True)
class AgentConfig:
    id: str
    title: str
    system_prompt: str
    model: str
    engine: str
    tools: tuple[str, ...] = ()


class ToolErrorBehavior(StrEnum):
    FAILED = "failed"
    RETRY = "retry"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class WorkConfig:
    """The ``[work]`` table: how this process runs turns."""

    max_turn_seconds: float = 1200.0
    """A turn's deadline, from its start: the whole run, whatever its calls take."""
    lease_seconds: float = 90.0
    """How long a turn is this process's without a heartbeat: a process that went away
    leaves its turns for this long at most."""
    heartbeat_seconds: float = 30.0
    """How often this process renews the leases of the turns it runs, all in one write."""
    drain_seconds: float = 30.0
    """How long a stopping process gives the turns it runs to finish, before it ends the rest
    as interrupted."""
    max_model_calls: int = 100
    """Calls to the model in one turn, past which the engine ends it."""
    tool_error_behavior: ToolErrorBehavior = ToolErrorBehavior.FAILED
    """What a tool's error does where the engine's framework leaves it open: back to the
    model as a failed result (``failed``), as a retry prompt that spends the tool's retries
    (``retry``), or the end of the turn (``error``)."""


@dataclass(frozen=True, slots=True)
class DatabaseConfig:
    """The ``[database]`` table: what one process takes of PostgreSQL. Beside the pool, a
    process holds two connections of its own, the listener and the work connection, so a
    fleet of N processes needs ``N × (pool_max + 2)`` connections, and a few more for
    ``robinauts db init`` and an operator."""

    pool_max: int = 10
    """The pool's connections at most: requests, turn events, finishes and the engines'
    checkpoints share them."""
    acquire_timeout_seconds: float = 5.0
    """How long an operation waits for a connection of the pool: a request then answers
    503, and a turn's runner tries its write again."""


@dataclass(frozen=True, slots=True)
class Config:
    providers: Mapping[str, ProviderConfig] = field(default_factory=dict)
    models: Mapping[str, ModelConfig] = field(default_factory=dict)
    tool_servers: Mapping[str, ToolServerConfig] = field(default_factory=dict)
    agents: Mapping[str, AgentConfig] = field(default_factory=dict)
    work: WorkConfig = field(default_factory=WorkConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)


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


@dataclass(frozen=True, slots=True)
class Swept:
    """What one sweep did. A task another process was sweeping at the time is named in
    ``skipped``, and left to it."""

    events: int = 0
    user_sessions: int = 0
    api_tokens: int = 0
    pending_logins: int = 0
    turns_ended: int = 0
    sessions_purged: int = 0
    skipped: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Readiness:
    """Whether this process should be sent requests, and if not, why not."""

    problems: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return not self.problems


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
    """When the run must have ended, whatever its lease: its start plus ``max_turn_seconds``."""
    worker_id: str | None = None
    """The process that holds the turn while it runs, and held it last once it has ended."""
    attempt: int = 1
    """Which holding of the turn this is; every write of its runner names it."""
    heartbeat_at: datetime | None = None
    """When its holder last renewed its lease."""
    cancel_requested_at: datetime | None = None
    """When somebody asked for it to stop, through any process: its holder stops it."""


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
