# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Where the LangChain engine keeps what it has: which sessions exist, and LangGraph's
checkpoints of each, through a saver whose thread is the session.

`SessionStore` is the shape. One implementation keeps it in this process, over LangGraph's own
in-memory saver. One keeps it in tables of the engine's own on PostgreSQL, made by `setup`,
named after the engine, referencing nothing of the controller's, over the saver in `saver.py`.
One keeps the same tables in a SQLite file of the worker's own, `FILE_NAME` in its folder, over
the saver in `sqlite_saver.py`. `forget` deletes everything held for a session."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from robinauts.agent_engines.contract.domain import SessionExistsError
from robinauts.agent_engines.langchain_engine.saver import CHECKPOINT_TABLES, PostgresSaver
from robinauts.agent_engines.langchain_engine.sqlite_file import SQLiteFile
from robinauts.agent_engines.langchain_engine.sqlite_saver import (
    CHECKPOINT_TABLES as SQLITE_CHECKPOINT_TABLES,
)
from robinauts.agent_engines.langchain_engine.sqlite_saver import SQLiteSaver, delete_thread

SESSIONS_TABLE = """
CREATE TABLE IF NOT EXISTS langgraph_sessions (
    session_id uuid PRIMARY KEY
);
"""


class SessionStore(ABC):
    """The engine's sessions, and the saver its graphs checkpoint through."""

    saver: BaseCheckpointSaver[str]

    @abstractmethod
    async def setup(self) -> None:
        """Make the store ready. Safe to repeat."""

    @abstractmethod
    async def create(self, session_id: uuid.UUID) -> None:
        """``SessionExistsError`` for a session that exists."""

    @abstractmethod
    async def exists(self, session_id: uuid.UUID) -> bool: ...

    @abstractmethod
    async def forget(self, session_id: uuid.UUID) -> None:
        """Delete the session and every checkpoint of its thread; nothing if it is gone."""

    async def close(self) -> None:
        """Let go of what the store opened; nothing for one that opens nothing."""
        return None


class InProcessSessionStore(SessionStore):
    """In this process."""

    def __init__(self) -> None:
        self.saver = InMemorySaver()
        self._known: set[uuid.UUID] = set()

    async def setup(self) -> None:
        # Nothing to set up: this store lives in the process and starts empty.
        pass

    async def create(self, session_id: uuid.UUID) -> None:
        if session_id in self._known:
            raise SessionExistsError(str(session_id))
        self._known.add(session_id)

    async def exists(self, session_id: uuid.UUID) -> bool:
        return session_id in self._known

    async def forget(self, session_id: uuid.UUID) -> None:
        self._known.discard(session_id)
        await self.saver.adelete_thread(str(session_id))


class PostgresSessionStore(SessionStore):
    """In the engine's own tables, through the pool it was given."""

    def __init__(self, pool: Any) -> None:
        self.saver = PostgresSaver(pool)
        self._pool = pool

    async def setup(self) -> None:
        await self._pool.execute(SESSIONS_TABLE + CHECKPOINT_TABLES)

    async def create(self, session_id: uuid.UUID) -> None:
        status = await self._pool.execute(
            "INSERT INTO langgraph_sessions (session_id) VALUES ($1) ON CONFLICT DO NOTHING",
            session_id,
        )
        if status.endswith(" 0"):
            raise SessionExistsError(str(session_id))

    async def exists(self, session_id: uuid.UUID) -> bool:
        found = await self._pool.fetchval(
            "SELECT 1 FROM langgraph_sessions WHERE session_id = $1", session_id
        )
        return found is not None

    async def forget(self, session_id: uuid.UUID) -> None:
        await self._pool.execute("DELETE FROM langgraph_sessions WHERE session_id = $1", session_id)
        await self.saver.adelete_thread(str(session_id))


FILE_NAME = "langchain.db"
"""The engine's file in the worker's folder."""

SQLITE_VERSION = 1
"""The version of the file's schema; frozen at 1 until the first release. A file made from
another edit of the schema is refused all the same."""

SQLITE_SCHEMA = """
CREATE TABLE langgraph_sessions (
    session_id text PRIMARY KEY
) STRICT;
""" + SQLITE_CHECKPOINT_TABLES


class SQLiteSessionStore(SessionStore):
    """In the engine's own file, ``FILE_NAME`` in ``folder``, which ``setup`` makes or checks
    and ``close`` lets go."""

    def __init__(self, folder: Path) -> None:
        self._file = SQLiteFile(
            folder / FILE_NAME,
            schema=SQLITE_SCHEMA,
            version=SQLITE_VERSION,
            tables=frozenset({"langgraph_sessions", "langgraph_checkpoints", "langgraph_writes"}),
        )
        self.saver = SQLiteSaver(self._file)

    async def setup(self) -> None:
        await self._file.open()

    async def create(self, session_id: uuid.UUID) -> None:
        added = await self._file.write(
            lambda connection: connection.execute(
                "INSERT INTO langgraph_sessions (session_id) VALUES (?) ON CONFLICT DO NOTHING",
                (str(session_id),),
            ).rowcount
        )
        if not added:
            raise SessionExistsError(str(session_id))

    async def exists(self, session_id: uuid.UUID) -> bool:
        found = await self._file.read(
            lambda connection: connection.execute(
                "SELECT 1 FROM langgraph_sessions WHERE session_id = ?", (str(session_id),)
            ).fetchone()
        )
        return found is not None

    async def forget(self, session_id: uuid.UUID) -> None:
        def forget(connection: Any) -> None:
            connection.execute(
                "DELETE FROM langgraph_sessions WHERE session_id = ?", (str(session_id),)
            )
            delete_thread(connection, str(session_id))

        await self._file.write(forget)

    async def close(self) -> None:
        await self._file.close()
