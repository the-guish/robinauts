# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What crosses the controller port, as plain data (``docs/architecture/controller.md``)."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
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
    max_output_tokens: int | None = None
    context_window: int | None = None
    title: str = ""


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
    # Tools of the server's that the agent is not given, by the server's own names.
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AgentConfig:
    id: str
    title: str
    system_prompt: str
    model: str
    engine: str
    tools: tuple[str, ...] = ()
    # How long one of its turns may run; left out, ``[work]``'s ``max_turn_seconds``.
    turn_timeout_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class WorkConfig:
    """``[work]``: how long a turn may run, and how a turn's lease is kept, in seconds; and
    how many turns a worker runs at once."""

    max_turn_seconds: float = 1200.0
    lease_seconds: float = 90.0
    heartbeat_seconds: float = 30.0
    sweep_seconds: float = 300.0
    stalled_after_seconds: float = 900.0
    max_running_tasks_per_worker: int = 100


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
    """Who a shell says is asking: a provider and a subject, as the provider names them, and
    what it said of them that the allow list reads: a verified email, Google's hosted domain,
    the groups."""

    provider: str
    subject: str
    name: str | None = None
    email: str | None = None
    hosted_domain: str | None = None
    groups: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class User:
    """A person; ``name``, ``email``, ``hosted_domain`` and ``groups`` are what their last
    sign-in said, which the allow list is checked against again on every use."""

    id: uuid.UUID
    provider: str
    subject: str
    name: str | None = None
    email: str | None = None
    created_at: datetime | None = None
    hosted_domain: str | None = None
    groups: tuple[str, ...] = ()


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


LAST_USED_EVERY = timedelta(minutes=1)
"""How often a device's ``last_used_at`` is written: by the resolve that finds it, when the
one recorded is this old or older, so the list says "last used" without a write on every
request."""

PAIRING_WRONG_TRIES = 3
"""The wrong numbers a pairing takes: the last of them deletes it."""


class PairingState(StrEnum):
    WAITING = "waiting"
    CLAIMED = "claimed"
    APPROVED = "approved"


class Approval(StrEnum):
    """What an approve with a number did (``Credentials.approve_pairing``)."""

    APPROVED = "approved"
    WRONG_NUMBER = "wrong_number"
    NOT_CLAIMED = "not_claimed"
    """Waiting for a phone still, or approved already: nothing to decide."""
    GONE = "gone"
    """Unknown, another's, expired, refused, or ended by this, the last wrong number."""


@dataclass(frozen=True, slots=True)
class DeviceDescription:
    """What a phone says of itself when it claims a pairing: data for the list, bounded by the
    shell, and never what anybody decides by."""

    name: str
    platform: str
    app_version: str | None = None


@dataclass(frozen=True, slots=True)
class DeviceSession:
    """A linked device. It holds the secret, and this the secret's SHA-256 hex: the claim hash
    of the pairing that linked it."""

    id: uuid.UUID
    user_id: uuid.UUID
    device: DeviceDescription
    secret_hash: str
    created_at: datetime
    last_used_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class Pairing:
    """A phone being linked (``docs/architecture/device-pairing.md``). Minted ``waiting`` under
    the hash of the code its QR carries; a claim adds the claim secret's hash, the number and
    the device's description; an approve moves the claim hash to the device session."""

    id: uuid.UUID
    user_id: uuid.UUID
    code_hash: str
    state: PairingState
    created_at: datetime
    expires_at: datetime
    claim_hash: str | None = None
    match_number: str | None = None
    wrong_tries: int = 0
    device: DeviceDescription | None = None


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
    ACTIVE = "active"
    """Not ended yet: queued or running, which its task says. A session has at most one."""
    FINISHED = "finished"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class TaskState(StrEnum):
    QUEUED = "queued"
    """Stored, and waiting for a worker to claim it."""
    RUNNING = "running"
    """Claimed by a worker, which holds its lease."""
    DONE = "done"
    """Needs no worker any more, however it ended."""


RUN_TURN = "run_turn"
"""The one kind of task: run a turn, named in the payload as ``turn_id``."""


@dataclass(frozen=True, slots=True)
class Task:
    """Work for a worker: what to do, and the lease of whoever does it."""

    id: uuid.UUID
    name: str
    payload: Mapping[str, Any]
    state: TaskState
    created_at: datetime
    lease_until: datetime
    """While queued, how long the task may wait; once claimed, the worker's lease."""
    claimed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Turn:
    """One question, and everything done to answer it. Its id is the run id on the wire."""

    id: uuid.UUID
    session_id: uuid.UUID
    follows: uuid.UUID
    model: str
    state: TurnState
    started_at: datetime
    task_id: uuid.UUID
    """The task that runs the turn, which holds its lease."""
    ended_at: datetime | None = None
    error: str | None = None
    """For the operator, on a turn that ended badly; never sent to a browser."""
    retries_message_id: uuid.UUID | None = None
    """The failed answer this turn tries again, which the model is told about."""


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


# --- records, as a store keeps them and a worker writes them ---------------------

Document = Mapping[str, Any]
"""A message or an event, whole, as the encoder wrote it."""

Cursor = tuple[datetime, uuid.UUID]
"""Where a page of sessions ended: the ``(updated_at, id)`` of its last session."""


@dataclass(frozen=True, slots=True)
class StoredMessage:
    """A message as a store keeps it: the columns it orders and joins by, and the document."""

    id: uuid.UUID
    session_id: uuid.UUID
    parent_id: uuid.UUID | None
    role: Role
    created_at: datetime
    document: Document


@dataclass(frozen=True, slots=True)
class StoredEvent:
    """One of a turn's events: its position, its document, and when it expires."""

    position: int
    document: Document
    expires_at: datetime
