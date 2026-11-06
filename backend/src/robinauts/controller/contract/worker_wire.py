# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The records the worker API carries, as JSON, and back.

The one encoding both ends share: web, which answers a sandbox worker, and the HTTP store the
sandbox worker is composed on. A session goes without its owner: the server takes the owner
from the sandbox's token and the session, never from what a worker sends, and a worker has no
use for it. Messages and events cross as the controller's documents, which this does not read.
Anything that does not decode is an ``InvalidValueError``.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from robinauts.controller.contract.domain import (
    InvalidValueError,
    LoadedTurn,
    Session,
    Task,
    TaskState,
    Turn,
    TurnState,
)

SANDBOX_HEADER = "Robinauts-Sandbox"
"""The header a sandbox worker names itself in: the id it minted when its process started."""

PREFIX = "/api/worker/v1"
"""Where the worker API's calls are, and nothing else."""

NO_OWNER = uuid.UUID(int=0)
"""The owner of a session as a sandbox worker holds it: nobody, since it is never told."""


def _time(value: object, what: str) -> datetime:
    if not isinstance(value, str):
        raise InvalidValueError(f"{what} is not a time: {value!r}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise InvalidValueError(f"{what} is not a time: {value!r}") from exc
    if parsed.tzinfo is None:
        raise InvalidValueError(f"{what} has no zone: {value!r}")
    return parsed


def _optional_time(value: object, what: str) -> datetime | None:
    return None if value is None else _time(value, what)


def _id(value: object, what: str) -> uuid.UUID:
    if not isinstance(value, str):
        raise InvalidValueError(f"{what} is not an id: {value!r}")
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise InvalidValueError(f"{what} is not an id: {value!r}") from exc


def _optional_id(value: object, what: str) -> uuid.UUID | None:
    return None if value is None else _id(value, what)


def _mapping(value: object, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidValueError(f"{what} is not an object")
    return value


def _text(value: object, what: str) -> str:
    if not isinstance(value, str):
        raise InvalidValueError(f"{what} is not text: {value!r}")
    return value


def _when(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def task_to_wire(task: Task) -> dict[str, Any]:
    return {
        "id": str(task.id),
        "name": task.name,
        "payload": dict(task.payload),
        "state": task.state.value,
        "created_at": _when(task.created_at),
        "lease_until": _when(task.lease_until),
        "claimed_at": _when(task.claimed_at),
        "sandboxed": task.sandboxed,
    }


def task_from_wire(value: object) -> Task:
    read = _mapping(value, "a task")
    try:
        state = TaskState(read["state"])
        return Task(
            _id(read["id"], "a task's id"),
            _text(read["name"], "a task's name"),
            dict(_mapping(read["payload"], "a task's payload")),
            state,
            _time(read["created_at"], "a task's created_at"),
            _time(read["lease_until"], "a task's lease_until"),
            _optional_time(read.get("claimed_at"), "a task's claimed_at"),
            bool(read.get("sandboxed", False)),
        )
    except (KeyError, ValueError) as exc:
        raise InvalidValueError(f"a task that does not decode: {exc}") from exc


def turn_to_wire(turn: Turn) -> dict[str, Any]:
    """Without its error, which is for the operator."""
    return {
        "id": str(turn.id),
        "session_id": str(turn.session_id),
        "follows": str(turn.follows),
        "model": turn.model,
        "state": turn.state.value,
        "started_at": _when(turn.started_at),
        "task_id": str(turn.task_id),
        "ended_at": _when(turn.ended_at),
        "retries_message_id": (
            None if turn.retries_message_id is None else str(turn.retries_message_id)
        ),
    }


def turn_from_wire(value: object) -> Turn:
    read = _mapping(value, "a turn")
    try:
        return Turn(
            _id(read["id"], "a turn's id"),
            _id(read["session_id"], "a turn's session_id"),
            _id(read["follows"], "a turn's follows"),
            _text(read["model"], "a turn's model"),
            TurnState(read["state"]),
            _time(read["started_at"], "a turn's started_at"),
            _id(read["task_id"], "a turn's task_id"),
            _optional_time(read.get("ended_at"), "a turn's ended_at"),
            None,
            _optional_id(read.get("retries_message_id"), "a turn's retries_message_id"),
        )
    except (KeyError, ValueError) as exc:
        raise InvalidValueError(f"a turn that does not decode: {exc}") from exc


def session_to_wire(session: Session) -> dict[str, Any]:
    """Without its owner."""
    return {
        "id": str(session.id),
        "agent": session.agent,
        "engine": session.engine,
        "created_at": _when(session.created_at),
        "updated_at": _when(session.updated_at),
        "title": session.title,
        "sandboxed": session.sandboxed,
        "sandbox_id": None if session.sandbox_id is None else str(session.sandbox_id),
    }


def session_from_wire(value: object, owner: uuid.UUID = NO_OWNER) -> Session:
    read = _mapping(value, "a session")
    try:
        return Session(
            _id(read["id"], "a session's id"),
            owner,
            _text(read["agent"], "a session's agent"),
            _text(read["engine"], "a session's engine"),
            _time(read["created_at"], "a session's created_at"),
            _time(read["updated_at"], "a session's updated_at"),
            _text(read.get("title", ""), "a session's title"),
            bool(read.get("sandboxed", False)),
            _optional_id(read.get("sandbox_id"), "a session's sandbox_id"),
        )
    except KeyError as exc:
        raise InvalidValueError(f"a session that does not decode: {exc}") from exc


def loaded_turn_to_wire(loaded: LoadedTurn) -> dict[str, Any]:
    return {
        "turn": turn_to_wire(loaded.turn),
        "session": session_to_wire(loaded.session),
        "messages": [dict(m) for m in loaded.messages],
    }


def loaded_turn_from_wire(value: object) -> LoadedTurn:
    read = _mapping(value, "a loaded turn")
    messages = read.get("messages")
    if not isinstance(messages, list):
        raise InvalidValueError("a loaded turn's messages are not a list")
    return LoadedTurn(
        turn_from_wire(read.get("turn")),
        session_from_wire(read.get("session")),
        tuple(_mapping(m, "a message") for m in messages),
    )
