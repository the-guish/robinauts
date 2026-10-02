# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Where the Pydantic AI engine keeps its memory: in this process, or in two tables of its
own on PostgreSQL, made by `setup`, named after the engine, referencing nothing of the
controller's. `forget` deletes everything held for a session."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter

from robinauts.agent_engines.contract.domain import SessionExistsError

TABLES = """
CREATE TABLE IF NOT EXISTS pydantic_ai_sessions (
    session_id uuid PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS pydantic_ai_checkpoints (
    session_id uuid NOT NULL REFERENCES pydantic_ai_sessions (session_id) ON DELETE CASCADE,
    checkpoint_id text NOT NULL,
    -- `json`, not `jsonb`: the framework's own rendering, kept as written.
    history json NOT NULL,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (session_id, checkpoint_id)
);
"""


class Memory:
    """The engine's memory, in the process."""

    def __init__(self) -> None:
        self._sessions: dict[uuid.UUID, dict[str, list[ModelMessage]]] = {}

    async def setup(self) -> None:
        pass

    async def create(self, session_id: uuid.UUID) -> None:
        if session_id in self._sessions:
            raise SessionExistsError(str(session_id))
        self._sessions[session_id] = {}

    async def exists(self, session_id: uuid.UUID) -> bool:
        return session_id in self._sessions

    async def history(self, session_id: uuid.UUID, checkpoint_id: str) -> list[ModelMessage] | None:
        return self._sessions[session_id].get(checkpoint_id)

    async def save(
        self, session_id: uuid.UUID, checkpoint_id: str, messages: list[ModelMessage]
    ) -> None:
        self._sessions[session_id][checkpoint_id] = messages

    async def forget(self, session_id: uuid.UUID) -> None:
        self._sessions.pop(session_id, None)


class PostgresMemory(Memory):
    """The same, in the engine's own tables, through the pool it was given."""

    def __init__(self, pool: Any) -> None:
        super().__init__()
        self._pool = pool

    async def setup(self) -> None:
        await self._pool.execute(TABLES)

    async def create(self, session_id: uuid.UUID) -> None:
        status = await self._pool.execute(
            "INSERT INTO pydantic_ai_sessions (session_id) VALUES ($1) ON CONFLICT DO NOTHING",
            session_id,
        )
        if status.endswith(" 0"):
            raise SessionExistsError(str(session_id))

    async def exists(self, session_id: uuid.UUID) -> bool:
        found = await self._pool.fetchval(
            "SELECT 1 FROM pydantic_ai_sessions WHERE session_id = $1", session_id
        )
        return found is not None

    async def history(self, session_id: uuid.UUID, checkpoint_id: str) -> list[ModelMessage] | None:
        rendered = await self._pool.fetchval(
            "SELECT history FROM pydantic_ai_checkpoints"
            " WHERE session_id = $1 AND checkpoint_id = $2",
            session_id,
            checkpoint_id,
        )
        return None if rendered is None else ModelMessagesTypeAdapter.validate_python(rendered)

    async def save(
        self, session_id: uuid.UUID, checkpoint_id: str, messages: list[ModelMessage]
    ) -> None:
        await self._pool.execute(
            "INSERT INTO pydantic_ai_checkpoints (session_id, checkpoint_id, history, created_at)"
            " VALUES ($1, $2, $3, $4)",
            session_id,
            checkpoint_id,
            ModelMessagesTypeAdapter.dump_python(messages, mode="json"),
            datetime.now(UTC),
        )

    async def forget(self, session_id: uuid.UUID) -> None:
        await self._pool.execute(
            "DELETE FROM pydantic_ai_sessions WHERE session_id = $1", session_id
        )
