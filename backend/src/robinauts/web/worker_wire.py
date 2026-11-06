# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What crosses between a worker in a process of its own and the server that spawned it: the
operations of ``WorkerCore`` as HTTP under ``PREFIX``, one route each, and their bodies.

Every route wants ``Authorization: Bearer`` and the secret the server handed its worker at
its start. Times are ISO 8601 with their offset, ids are strings, and a message or an event is
its document, as the store keeps it. The two refusals a worker acts on are a body with
``error``: ``TURN_LOST`` is 409, ``SESSION_NOT_FOUND`` 404. None of it is in the OpenAPI
document: it is the server's own, not the frontend's.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import AwareDatetime, BaseModel, Field

from robinauts.controller.contract.domain import (
    Role,
    Session,
    StoredEvent,
    StoredMessage,
    Task,
    TaskState,
    Turn,
    TurnState,
)

PREFIX = "/internal/worker"

TURN_LOST = "turn_lost"
SESSION_NOT_FOUND = "session_not_found"

MAX_WAIT_SECONDS = 30.0
"""The longest a wait for a queued task, or for a cancel, holds its request."""

MAX_CLAIM = 100
"""The most tasks one claim asks for."""


class TaskWire(BaseModel):
    id: uuid.UUID
    name: str
    payload: dict[str, Any]
    state: TaskState
    created_at: AwareDatetime
    lease_until: AwareDatetime
    claimed_at: AwareDatetime | None = None

    @classmethod
    def of(cls, task: Task) -> TaskWire:
        return cls(
            id=task.id,
            name=task.name,
            payload=dict(task.payload),
            state=task.state,
            created_at=task.created_at,
            lease_until=task.lease_until,
            claimed_at=task.claimed_at,
        )

    def domain(self) -> Task:
        return Task(
            self.id,
            self.name,
            self.payload,
            self.state,
            self.created_at,
            self.lease_until,
            self.claimed_at,
        )


class TurnWire(BaseModel):
    id: uuid.UUID
    session_id: uuid.UUID
    follows: uuid.UUID
    model: str
    state: TurnState
    started_at: AwareDatetime
    task_id: uuid.UUID
    ended_at: AwareDatetime | None = None
    error: str | None = None
    retries_message_id: uuid.UUID | None = None

    @classmethod
    def of(cls, turn: Turn) -> TurnWire:
        return cls(
            id=turn.id,
            session_id=turn.session_id,
            follows=turn.follows,
            model=turn.model,
            state=turn.state,
            started_at=turn.started_at,
            task_id=turn.task_id,
            ended_at=turn.ended_at,
            error=turn.error,
            retries_message_id=turn.retries_message_id,
        )

    def domain(self) -> Turn:
        return Turn(
            self.id,
            self.session_id,
            self.follows,
            self.model,
            self.state,
            self.started_at,
            self.task_id,
            self.ended_at,
            self.error,
            self.retries_message_id,
        )


class SessionWire(BaseModel):
    id: uuid.UUID
    owner_id: uuid.UUID
    agent: str
    engine: str
    created_at: AwareDatetime
    updated_at: AwareDatetime
    title: str = ""

    @classmethod
    def of(cls, session: Session) -> SessionWire:
        return cls(
            id=session.id,
            owner_id=session.owner_id,
            agent=session.agent,
            engine=session.engine,
            created_at=session.created_at,
            updated_at=session.updated_at,
            title=session.title,
        )

    def domain(self) -> Session:
        return Session(
            self.id,
            self.owner_id,
            self.agent,
            self.engine,
            self.created_at,
            self.updated_at,
            self.title,
        )


class StoredMessageWire(BaseModel):
    id: uuid.UUID
    session_id: uuid.UUID
    parent_id: uuid.UUID | None
    role: Role
    created_at: AwareDatetime
    document: dict[str, Any]

    @classmethod
    def of(cls, message: StoredMessage) -> StoredMessageWire:
        return cls(
            id=message.id,
            session_id=message.session_id,
            parent_id=message.parent_id,
            role=message.role,
            created_at=message.created_at,
            document=dict(message.document),
        )

    def domain(self) -> StoredMessage:
        return StoredMessage(
            self.id, self.session_id, self.parent_id, self.role, self.created_at, self.document
        )


class StoredEventWire(BaseModel):
    position: int
    document: dict[str, Any]
    expires_at: AwareDatetime

    @classmethod
    def of(cls, event: StoredEvent) -> StoredEventWire:
        return cls(
            position=event.position, document=dict(event.document), expires_at=event.expires_at
        )

    def domain(self) -> StoredEvent:
        return StoredEvent(self.position, self.document, self.expires_at)


# --- the bodies of the routes --------------------------------------------------


class ClaimRequest(BaseModel):
    now: AwareDatetime
    until: AwareDatetime
    limit: int = Field(ge=1, le=MAX_CLAIM)


class TasksResponse(BaseModel):
    tasks: list[TaskWire]


class WaitRequest(BaseModel):
    now: AwareDatetime
    timeout: float = Field(ge=0, le=MAX_WAIT_SECONDS)


class WaitResponse(BaseModel):
    queued: bool


class RenewRequest(BaseModel):
    tasks: list[uuid.UUID]
    now: AwareDatetime
    until: AwareDatetime


class RenewResponse(BaseModel):
    stop: list[uuid.UUID]
    """The tasks asked to stop, which a stop missed."""


class TaskResponse(BaseModel):
    task: TaskWire | None


class FoundTurnResponse(BaseModel):
    owner: uuid.UUID | None
    turn: TurnWire | None


class SessionsResponse(BaseModel):
    sessions: list[SessionWire]


class MessagesResponse(BaseModel):
    messages: list[dict[str, Any]]


class AppendRequest(BaseModel):
    document: dict[str, Any]
    written_at: AwareDatetime
    expires_at: AwareDatetime


class FinishRequest(BaseModel):
    state: TurnState
    ended_at: AwareDatetime
    error: str | None
    answer: StoredMessageWire | None
    events: list[StoredEventWire]
    updated_at: AwareDatetime


class CancelsResponse(BaseModel):
    cursor: int
    """Where the next poll goes on from."""
    tasks: list[uuid.UUID]
    """The tasks asked to stop past the cursor asked with."""


class WorkerRefusal(BaseModel):
    error: str
    detail: str
