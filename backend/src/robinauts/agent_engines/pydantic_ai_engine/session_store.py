# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Where the Pydantic AI engine keeps what it has: which sessions exist, and the history
of each at every checkpoint.

`SessionStore` is the shape. One implementation keeps it in this process. One keeps it in two
tables of the engine's own on PostgreSQL, made by `setup`, named after the engine, referencing
nothing of the controller's. One keeps the same tables in a SQLite file of the worker's own,
`FILE_NAME` in its folder. `forget` deletes everything held for a session."""

from __future__ import annotations

import sqlite3
import uuid
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter

from robinauts.agent_engines.contract.domain import SessionExistsError
from robinauts.agent_engines.pydantic_ai_engine.sqlite_file import SQLiteFile

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


class SessionStore(ABC):
    """The engine's sessions, and the history each has at each checkpoint."""

    @abstractmethod
    async def setup(self) -> None:
        """Make the store ready. Safe to repeat."""

    @abstractmethod
    async def create(self, session_id: uuid.UUID) -> None:
        """``SessionExistsError`` for a session that exists."""

    @abstractmethod
    async def exists(self, session_id: uuid.UUID) -> bool: ...

    @abstractmethod
    async def history(self, session_id: uuid.UUID, checkpoint_id: str) -> list[ModelMessage] | None:
        """The history at that checkpoint, or ``None`` for one the session does not have."""

    @abstractmethod
    async def save(
        self, session_id: uuid.UUID, checkpoint_id: str, messages: list[ModelMessage]
    ) -> None:
        """Keep the history under that checkpoint, in place of what it held."""

    @abstractmethod
    async def latest(self, session_id: uuid.UUID) -> tuple[str, list[ModelMessage]] | None:
        """The checkpoint saved last, and its history; ``None`` for a session with none."""

    @abstractmethod
    async def forget(self, session_id: uuid.UUID) -> None:
        """Delete the session and every history of it; nothing if it is gone."""

    async def close(self) -> None:
        """Let go of what the store opened; nothing for one that opens nothing."""
        return None


class InProcessSessionStore(SessionStore):
    """In this process."""

    def __init__(self) -> None:
        self._sessions: dict[uuid.UUID, dict[str, list[ModelMessage]]] = {}

    async def setup(self) -> None:
        # Nothing to set up: this store lives in the process and starts empty.
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
        # Moved to the end, which is where `latest` looks.
        self._sessions[session_id].pop(checkpoint_id, None)
        self._sessions[session_id][checkpoint_id] = messages

    async def latest(self, session_id: uuid.UUID) -> tuple[str, list[ModelMessage]] | None:
        return next(reversed(self._sessions[session_id].items()), None)

    async def forget(self, session_id: uuid.UUID) -> None:
        self._sessions.pop(session_id, None)


class PostgresSessionStore(SessionStore):
    """In the engine's own tables, through the pool it was given."""

    def __init__(self, pool: Any) -> None:
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
            " VALUES ($1, $2, $3, $4) ON CONFLICT (session_id, checkpoint_id)"
            " DO UPDATE SET history = excluded.history, created_at = excluded.created_at",
            session_id,
            checkpoint_id,
            ModelMessagesTypeAdapter.dump_python(messages, mode="json"),
            datetime.now(UTC),
        )

    async def latest(self, session_id: uuid.UUID) -> tuple[str, list[ModelMessage]] | None:
        row = await self._pool.fetchrow(
            "SELECT checkpoint_id, history FROM pydantic_ai_checkpoints WHERE session_id = $1"
            " ORDER BY created_at DESC LIMIT 1",
            session_id,
        )
        if row is None:
            return None
        return row["checkpoint_id"], ModelMessagesTypeAdapter.validate_python(row["history"])

    async def forget(self, session_id: uuid.UUID) -> None:
        await self._pool.execute(
            "DELETE FROM pydantic_ai_sessions WHERE session_id = $1", session_id
        )


FILE_NAME = "pydantic-ai.db"
"""The engine's file in the worker's folder."""

SQLITE_VERSION = 1
"""The version of the file's schema; frozen at 1 until the first release. A file made from
another edit of the schema is refused all the same."""

SQLITE_SCHEMA = """
CREATE TABLE pydantic_ai_sessions (
    session_id text PRIMARY KEY
) STRICT;
CREATE TABLE pydantic_ai_checkpoints (
    session_id text NOT NULL REFERENCES pydantic_ai_sessions (session_id) ON DELETE CASCADE,
    checkpoint_id text NOT NULL,
    -- The framework's own JSON rendering, kept as written.
    history text NOT NULL,
    -- The order of the saves in the session: the last is the greatest.
    saved integer NOT NULL,
    PRIMARY KEY (session_id, checkpoint_id)
) STRICT;
"""


def _render(messages: list[ModelMessage]) -> str:
    return ModelMessagesTypeAdapter.dump_json(messages).decode("utf-8")


def _parse(rendered: str) -> list[ModelMessage]:
    return ModelMessagesTypeAdapter.validate_json(rendered)


class SQLiteSessionStore(SessionStore):
    """In the engine's own file, ``FILE_NAME`` in ``folder``, which ``setup`` makes or checks
    and ``close`` lets go."""

    def __init__(self, folder: Path) -> None:
        self._file = SQLiteFile(
            folder / FILE_NAME,
            schema=SQLITE_SCHEMA,
            version=SQLITE_VERSION,
            tables=frozenset({"pydantic_ai_sessions", "pydantic_ai_checkpoints"}),
        )

    async def setup(self) -> None:
        await self._file.open()

    async def create(self, session_id: uuid.UUID) -> None:
        added = await self._file.write(
            lambda connection: connection.execute(
                "INSERT INTO pydantic_ai_sessions (session_id) VALUES (?) ON CONFLICT DO NOTHING",
                (str(session_id),),
            ).rowcount
        )
        if not added:
            raise SessionExistsError(str(session_id))

    async def exists(self, session_id: uuid.UUID) -> bool:
        found = await self._file.read(
            lambda connection: connection.execute(
                "SELECT 1 FROM pydantic_ai_sessions WHERE session_id = ?", (str(session_id),)
            ).fetchone()
        )
        return found is not None

    async def history(self, session_id: uuid.UUID, checkpoint_id: str) -> list[ModelMessage] | None:
        row = await self._file.read(
            lambda connection: connection.execute(
                "SELECT history FROM pydantic_ai_checkpoints"
                " WHERE session_id = ? AND checkpoint_id = ?",
                (str(session_id), checkpoint_id),
            ).fetchone()
        )
        return None if row is None else _parse(row[0])

    async def save(
        self, session_id: uuid.UUID, checkpoint_id: str, messages: list[ModelMessage]
    ) -> None:
        rendered = _render(messages)

        def save(connection: sqlite3.Connection) -> None:
            connection.execute(
                "INSERT INTO pydantic_ai_checkpoints (session_id, checkpoint_id, history, saved)"
                " VALUES (:session, :checkpoint, :history, (SELECT coalesce(max(saved), 0) + 1"
                "   FROM pydantic_ai_checkpoints WHERE session_id = :session))"
                " ON CONFLICT (session_id, checkpoint_id)"
                " DO UPDATE SET history = excluded.history, saved = excluded.saved",
                {"session": str(session_id), "checkpoint": checkpoint_id, "history": rendered},
            )

        await self._file.write(save)

    async def latest(self, session_id: uuid.UUID) -> tuple[str, list[ModelMessage]] | None:
        row = await self._file.read(
            lambda connection: connection.execute(
                "SELECT checkpoint_id, history FROM pydantic_ai_checkpoints WHERE session_id = ?"
                " ORDER BY saved DESC LIMIT 1",
                (str(session_id),),
            ).fetchone()
        )
        return None if row is None else (row[0], _parse(row[1]))

    async def forget(self, session_id: uuid.UUID) -> None:
        await self._file.write(
            lambda connection: connection.execute(
                "DELETE FROM pydantic_ai_sessions WHERE session_id = ?", (str(session_id),)
            )
        )

    async def close(self) -> None:
        await self._file.close()
