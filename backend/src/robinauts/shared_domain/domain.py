# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The records more than one package uses: the errors they share, the configuration, the storage
asked for, a session with its messages and turns, and the events a turn is told as."""

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


class SessionNotFoundError(ControllerError):
    """Also for a session that exists and is not the user's."""


class ProjectNotFoundError(ControllerError):
    """Also for a project that exists and is not the user's."""


class UnknownEngineError(ControllerError):
    """An agent configured on an engine this build does not have."""


class UnreachableProviderError(ControllerError):
    """A model on a provider kind its agent's engine cannot reach."""


class MissingSecretError(ControllerError):
    """A key or a secret the environment does not hold."""


class TurnLostError(ControllerError):
    """A runner's write refused: the turn is no longer running, its lease has passed, or
    another runner holds the position. The runner writes nothing more."""


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


DEFAULT_WORKER = "default"
"""The worker an agent's turns run on when its configuration names none."""


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
    # The worker its turns run on, a process of its own on PostgreSQL; left out, the default.
    worker: str = DEFAULT_WORKER


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
    commands: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    """By worker, the command that starts it in a process of its own, for the workers whose
    ``[workers.<name>]`` table names one. The others start as ``robinauts.cli.worker_process``.
    A command differs in nothing else: it is handed the same environment, the same secret and
    the same turns, say ``docker run -i`` of an image that runs an engine this build lacks."""

    def workers(self) -> tuple[str, ...]:
        """Every worker the agents name: the default first, which always runs, then the
        others by name."""
        named = {agent.worker for agent in self.agents.values()} - {DEFAULT_WORKER}
        return (DEFAULT_WORKER, *sorted(named))

    def worker_of(self, agent: str) -> str:
        """The worker that runs the turns of that agent: the default for one no longer
        configured, whose turns fail there rather than wait for a worker nobody runs."""
        found = self.agents.get(agent)
        return DEFAULT_WORKER if found is None else found.worker

    def for_worker(self, worker: str) -> Config:
        """This configuration with the agents of that worker alone: what it builds engines
        for and runs turns of."""
        agents = {id_: a for id_, a in self.agents.items() if a.worker == worker}
        return Config(
            self.providers, self.models, self.tool_servers, agents, self.work, self.commands
        )

    def command_of(self, worker: str) -> tuple[str, ...] | None:
        """The command that starts that worker, or ``None`` for one that starts as the rest."""
        return self.commands.get(worker)


class StorageKind(StrEnum):
    POSTGRES = "postgres"
    LOCAL = "local"
    IN_PROCESS = "in-process"


@dataclass(frozen=True, slots=True)
class StorageConfig:
    """A database URL, a folder, or nothing, for a test or a local start."""

    kind: StorageKind
    url: str | None = None
    path: str | None = None


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


TASK_KEY = "task_key"
"""The one key a task payload must hold: a string naming what kind of task it is, with its
version (``code_task/1``), which an engine reads first and acts on only when it knows it."""


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
    task_payload: Mapping[str, Any] | None = None
    """A question's structured data beside its text, for the engine rather than the model: a
    JSON object whose ``task_key`` names the kind of task; untyped past that. ``None`` on an
    answer, and on a question asked with text alone."""


DEFAULT_PROJECT = "Default"
"""The name of the project every owner has, which a session is in unless another is chosen."""


@dataclass(frozen=True, slots=True)
class Project:
    """A named group of one owner's sessions. Each owner's names are unique."""

    id: uuid.UUID
    owner_id: uuid.UUID
    name: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Session:
    id: uuid.UUID
    owner_id: uuid.UUID
    agent: str
    engine: str
    """The engine that holds the session's state: the agent's when the session was made."""

    created_at: datetime
    updated_at: datetime
    title: str = ""
    project_id: uuid.UUID = field(kw_only=True)
    """The owner's project the session is in: ``DEFAULT_PROJECT`` unless another was chosen."""
    archived_at: datetime | None = field(default=None, kw_only=True)
    """When the owner archived it, or ``None``. An archived session is left out of the listing,
    and keeps everything else. Its next turn unarchives it."""


class TurnState(StrEnum):
    ACTIVE = "active"
    """Not ended yet: queued or running, which its task says. A session has at most one."""
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
    task_id: uuid.UUID
    """The task that runs the turn, which holds its lease."""
    ended_at: datetime | None = None
    error: str | None = None
    """For the operator, on a turn that ended badly; never sent to a browser."""
    retries_message_id: uuid.UUID | None = None
    """The failed answer this turn tries again, which the model is told about."""


# --- turn events ------------------------------------------------------------------


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


@dataclass(frozen=True, slots=True)
class TurnStarted:
    session_id: uuid.UUID
    turn_id: uuid.UUID
    question: Message


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
