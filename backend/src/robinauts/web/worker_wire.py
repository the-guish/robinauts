# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What crosses between a worker in a process of its own and the server that spawned it: the
operations of ``TurnBroker`` as HTTP under ``PREFIX``, one route each, and their bodies.

Every route wants ``Authorization: Bearer`` and the secret the server handed its worker at
its start. Times are ISO 8601 with their offset, ids are strings, and a message or an event is
its document, as the store keeps it. A task, a turn, a session, a stored message and a stored
event cross as the contract's own dataclasses, which pydantic writes and reads field by field.
The two refusals a worker acts on are a body with ``error``: ``TURN_LOST`` is 409,
``SESSION_NOT_FOUND`` 404. None of it is in the OpenAPI document: it is the server's own, not
the frontend's.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import AwareDatetime, BaseModel, Field

from robinauts.controller.contract.domain import (
    Session,
    StoredEvent,
    StoredMessage,
    Task,
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

SERVER_KEEP_ALIVE_SECONDS = 5
"""How long the server keeps an idle connection open before it closes it: uvicorn's
``timeout_keep_alive``, which ``robinauts start`` sets."""

WORKER_KEEP_ALIVE_SECONDS = 2.0
"""How long a worker reuses an idle connection: well under ``SERVER_KEEP_ALIVE_SECONDS``. With
the two equal, a request sent on a connection just as the server closes it fails, "Server
disconnected" or reset, and the turn it writes for with it."""


# --- the bodies of the routes --------------------------------------------------


class ClaimRequest(BaseModel):
    now: AwareDatetime
    until: AwareDatetime
    limit: int = Field(ge=1, le=MAX_CLAIM)


class TasksResponse(BaseModel):
    tasks: list[Task]


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
    task: Task | None


class FoundTurnResponse(BaseModel):
    owner: uuid.UUID | None
    turn: Turn | None


class SessionResponse(BaseModel):
    session: Session


class SessionsResponse(BaseModel):
    sessions: list[Session]


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
    answer: StoredMessage | None
    events: list[StoredEvent]
    updated_at: AwareDatetime


class CancelsResponse(BaseModel):
    cursor: int
    """Where the next poll goes on from."""
    tasks: list[uuid.UUID]
    """The tasks asked to stop past the cursor asked with."""


class WorkerRefusal(BaseModel):
    error: str
    detail: str
