# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A LangGraph checkpoint saver over asyncpg, and the engine's sessions beside it.

Ours because `langgraph-checkpoint-postgres` depends on `psycopg`, which is LGPL
(ADR 0002). Three tables, made by `setup`, named after the engine, referencing nothing of
the controller's: the sessions, the checkpoints and the pending writes. A checkpoint and
its metadata are written with the saver's own serializer (`dumps_typed`), as bytes beside
their type, never pickled by us. The async methods are the ones the engine runs on; the
sync ones stay the base class's, which refuse.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
    get_checkpoint_metadata,
)

from robinauts.agent_engines.contract.domain import SessionExistsError

TABLES = """
CREATE TABLE IF NOT EXISTS langgraph_sessions (
    session_id uuid PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS langgraph_checkpoints (
    thread_id text NOT NULL,
    checkpoint_ns text NOT NULL DEFAULT '',
    checkpoint_id text NOT NULL,
    parent_checkpoint_id text,
    checkpoint_type text NOT NULL,
    checkpoint bytea NOT NULL,
    metadata_type text NOT NULL,
    metadata bytea NOT NULL,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);
CREATE TABLE IF NOT EXISTS langgraph_writes (
    thread_id text NOT NULL,
    checkpoint_ns text NOT NULL DEFAULT '',
    checkpoint_id text NOT NULL,
    task_id text NOT NULL,
    idx integer NOT NULL,
    channel text NOT NULL,
    value_type text NOT NULL,
    value bytea NOT NULL,
    task_path text NOT NULL DEFAULT '',
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);
"""

_CHECKPOINT_COLUMNS = (
    "thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, checkpoint_type,"
    " checkpoint, metadata_type, metadata"
)


class PostgresSessions:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def setup(self) -> None:
        await self._pool.execute(TABLES)

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


class PostgresSaver(BaseCheckpointSaver[str]):
    def __init__(self, pool: Any) -> None:
        super().__init__()
        self._pool = pool

    async def setup(self) -> None:
        await self._pool.execute(TABLES)

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = get_checkpoint_id(config)
        if checkpoint_id:
            row = await self._pool.fetchrow(
                f"SELECT {_CHECKPOINT_COLUMNS} FROM langgraph_checkpoints"
                " WHERE thread_id = $1 AND checkpoint_ns = $2 AND checkpoint_id = $3",
                thread_id,
                checkpoint_ns,
                checkpoint_id,
            )
        else:
            row = await self._pool.fetchrow(
                f"SELECT {_CHECKPOINT_COLUMNS} FROM langgraph_checkpoints"
                " WHERE thread_id = $1 AND checkpoint_ns = $2"
                " ORDER BY checkpoint_id DESC LIMIT 1",
                thread_id,
                checkpoint_ns,
            )
        return None if row is None else await self._tuple(row)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        thread_id = config["configurable"]["thread_id"] if config else None
        checkpoint_ns = config["configurable"].get("checkpoint_ns") if config else None
        checkpoint_id = get_checkpoint_id(config) if config else None
        before_id = get_checkpoint_id(before) if before else None
        rows = await self._pool.fetch(
            f"SELECT {_CHECKPOINT_COLUMNS} FROM langgraph_checkpoints"
            " WHERE ($1::text IS NULL OR thread_id = $1)"
            "   AND ($2::text IS NULL OR checkpoint_ns = $2)"
            "   AND ($3::text IS NULL OR checkpoint_id = $3)"
            "   AND ($4::text IS NULL OR checkpoint_id < $4)"
            " ORDER BY thread_id, checkpoint_ns, checkpoint_id DESC",
            thread_id,
            checkpoint_ns,
            checkpoint_id or None,
            before_id or None,
        )
        for row in rows:
            found = await self._tuple(row)
            if filter and not all(found.metadata.get(k) == v for k, v in filter.items()):
                continue
            if limit is not None:
                if limit <= 0:
                    break
                limit -= 1
            yield found

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_type, checkpoint_blob = self.serde.dumps_typed(checkpoint)
        metadata_type, metadata_blob = self.serde.dumps_typed(
            get_checkpoint_metadata(config, metadata)
        )
        await self._pool.execute(
            f"INSERT INTO langgraph_checkpoints ({_CHECKPOINT_COLUMNS})"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, $8)"
            " ON CONFLICT (thread_id, checkpoint_ns, checkpoint_id) DO UPDATE"
            " SET parent_checkpoint_id = EXCLUDED.parent_checkpoint_id,"
            "     checkpoint_type = EXCLUDED.checkpoint_type, checkpoint = EXCLUDED.checkpoint,"
            "     metadata_type = EXCLUDED.metadata_type, metadata = EXCLUDED.metadata",
            thread_id,
            checkpoint_ns,
            checkpoint["id"],
            config["configurable"].get("checkpoint_id"),
            checkpoint_type,
            checkpoint_blob,
            metadata_type,
            metadata_blob,
        )
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint["id"],
            }
        }

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = config["configurable"]["checkpoint_id"]
        for position, (channel, value) in enumerate(writes):
            idx = WRITES_IDX_MAP.get(channel, position)
            value_type, blob = self.serde.dumps_typed(value)
            # A write at a position already held is kept; a special one is replaced, as
            # the in-memory saver has it.
            conflict = (
                "DO UPDATE SET channel = EXCLUDED.channel, value_type = EXCLUDED.value_type,"
                " value = EXCLUDED.value, task_path = EXCLUDED.task_path"
                if idx < 0
                else "DO NOTHING"
            )
            await self._pool.execute(
                "INSERT INTO langgraph_writes (thread_id, checkpoint_ns, checkpoint_id, task_id,"
                " idx, channel, value_type, value, task_path)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)"
                f" ON CONFLICT (thread_id, checkpoint_ns, checkpoint_id, task_id, idx) {conflict}",
                thread_id,
                checkpoint_ns,
                checkpoint_id,
                task_id,
                idx,
                channel,
                value_type,
                blob,
                task_path,
            )

    async def adelete_thread(self, thread_id: str) -> None:
        async with self._pool.acquire() as connection, connection.transaction():
            await connection.execute("DELETE FROM langgraph_writes WHERE thread_id = $1", thread_id)
            await connection.execute(
                "DELETE FROM langgraph_checkpoints WHERE thread_id = $1", thread_id
            )

    def get_next_version(self, current: str | None, channel: None) -> str:
        if current is None:
            current_v = 0
        elif isinstance(current, int):
            current_v = current
        else:
            current_v = int(current.split(".")[0])
        return f"{current_v + 1:032}.{random.random():016}"

    async def _tuple(self, row: Any) -> CheckpointTuple:
        thread_id, checkpoint_ns = row["thread_id"], row["checkpoint_ns"]
        checkpoint_id, parent = row["checkpoint_id"], row["parent_checkpoint_id"]
        writes = await self._pool.fetch(
            "SELECT task_id, channel, value_type, value FROM langgraph_writes"
            " WHERE thread_id = $1 AND checkpoint_ns = $2 AND checkpoint_id = $3"
            " ORDER BY task_id, idx",
            thread_id,
            checkpoint_ns,
            checkpoint_id,
        )
        return CheckpointTuple(
            config={
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": checkpoint_id,
                }
            },
            checkpoint=self.serde.loads_typed((row["checkpoint_type"], row["checkpoint"])),
            metadata=self.serde.loads_typed((row["metadata_type"], row["metadata"])),
            parent_config=(
                {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": checkpoint_ns,
                        "checkpoint_id": parent,
                    }
                }
                if parent
                else None
            ),
            pending_writes=[
                (w["task_id"], w["channel"], self.serde.loads_typed((w["value_type"], w["value"])))
                for w in writes
            ],
        )
