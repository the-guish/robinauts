# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Where the LangChain engine keeps what it has: which sessions exist, and LangGraph's
checkpoints of each, through a saver whose thread is the session.

`Memory` is the shape; one implementation keeps it in this process, over LangGraph's own
in-memory saver, and one in tables of the engine's own on PostgreSQL, made by `setup`,
named after the engine, referencing nothing of the controller's, over the saver in
`saver.py`. `forget` deletes everything held for a session."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from robinauts.agent_engines.contract.domain import SessionExistsError
from robinauts.agent_engines.langchain_engine.saver import CHECKPOINT_TABLES, PostgresSaver

SESSIONS_TABLE = """
CREATE TABLE IF NOT EXISTS langgraph_sessions (
    session_id uuid PRIMARY KEY
);
"""


class Memory(ABC):
    """The engine's sessions, and the saver its graphs checkpoint through."""

    saver: BaseCheckpointSaver[str]

    @abstractmethod
    async def setup(self) -> None:
        """Make the memory ready. Safe to repeat."""

    @abstractmethod
    async def create(self, session_id: uuid.UUID) -> None:
        """``SessionExistsError`` for a session that exists."""

    @abstractmethod
    async def exists(self, session_id: uuid.UUID) -> bool: ...

    @abstractmethod
    async def forget(self, session_id: uuid.UUID) -> None:
        """Delete the session and every checkpoint of its thread; nothing if it is gone."""


class InProcessMemory(Memory):
    """In this process."""

    def __init__(self) -> None:
        self.saver = InMemorySaver()
        self._known: set[uuid.UUID] = set()

    async def setup(self) -> None:
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


class PostgresMemory(Memory):
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
