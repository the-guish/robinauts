# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A LangGraph checkpoint saver over the standard library's ``sqlite3``, for the engine's
sessions in a file of the worker's own.

Ours, like the PostgreSQL one in ``saver.py``: it needs no third-party code. It keeps the same
two tables, in the file that ``SQLiteSessionStore`` opens beside its own table. A checkpoint
and its metadata are written with the saver's own serializer (``dumps_typed``), as bytes beside
their type. Serializing happens on the event loop; the file's thread reads and writes rows. The
async methods are the ones the engine runs on; the sync ones stay the base class's, which
refuse.
"""

from __future__ import annotations

import sqlite3
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

from robinauts.agent_engines.langchain_engine.saver import next_version
from robinauts.agent_engines.langchain_engine.sqlite_file import SQLiteFile

CHECKPOINT_TABLES = """
CREATE TABLE langgraph_checkpoints (
    thread_id text NOT NULL,
    checkpoint_ns text NOT NULL DEFAULT '',
    checkpoint_id text NOT NULL,
    parent_checkpoint_id text,
    checkpoint_type text NOT NULL,
    checkpoint blob NOT NULL,
    metadata_type text NOT NULL,
    metadata blob NOT NULL,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
) STRICT;
CREATE TABLE langgraph_writes (
    thread_id text NOT NULL,
    checkpoint_ns text NOT NULL DEFAULT '',
    checkpoint_id text NOT NULL,
    task_id text NOT NULL,
    idx integer NOT NULL,
    channel text NOT NULL,
    value_type text NOT NULL,
    value blob NOT NULL,
    task_path text NOT NULL DEFAULT '',
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
) STRICT;
"""

_CHECKPOINT_COLUMNS = (
    "thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, checkpoint_type,"
    " checkpoint, metadata_type, metadata"
)

Row = tuple[Any, ...]
"""A checkpoint's row, in ``_CHECKPOINT_COLUMNS``' order."""

Found = tuple[Row, list[Row]]
"""A checkpoint's row, and its pending writes: task id, channel, value type, value."""


def _writes_of(connection: sqlite3.Connection, row: Row) -> list[Row]:
    return connection.execute(
        "SELECT task_id, channel, value_type, value FROM langgraph_writes"
        " WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?"
        " ORDER BY task_id, idx",
        row[:3],
    ).fetchall()


class SQLiteSaver(BaseCheckpointSaver[str]):
    """Over the file it is given; its tables are made by ``SQLiteSessionStore``'s file."""

    def __init__(self, file: SQLiteFile) -> None:
        super().__init__()
        self._file = file

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = get_checkpoint_id(config)

        def look(connection: sqlite3.Connection) -> Found | None:
            if checkpoint_id:
                row = connection.execute(
                    f"SELECT {_CHECKPOINT_COLUMNS} FROM langgraph_checkpoints"
                    " WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?",
                    (thread_id, checkpoint_ns, checkpoint_id),
                ).fetchone()
            else:
                row = connection.execute(
                    f"SELECT {_CHECKPOINT_COLUMNS} FROM langgraph_checkpoints"
                    " WHERE thread_id = ? AND checkpoint_ns = ?"
                    " ORDER BY checkpoint_id DESC LIMIT 1",
                    (thread_id, checkpoint_ns),
                ).fetchone()
            return None if row is None else (row, _writes_of(connection, row))

        found = await self._file.read(look)
        return None if found is None else self._tuple(*found)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        given = {
            "thread": config["configurable"]["thread_id"] if config else None,
            "ns": config["configurable"].get("checkpoint_ns") if config else None,
            "id": (get_checkpoint_id(config) if config else None) or None,
            "before": (get_checkpoint_id(before) if before else None) or None,
        }

        def look(connection: sqlite3.Connection) -> list[Found]:
            rows = connection.execute(
                f"SELECT {_CHECKPOINT_COLUMNS} FROM langgraph_checkpoints"
                " WHERE (:thread IS NULL OR thread_id = :thread)"
                "   AND (:ns IS NULL OR checkpoint_ns = :ns)"
                "   AND (:id IS NULL OR checkpoint_id = :id)"
                "   AND (:before IS NULL OR checkpoint_id < :before)"
                " ORDER BY thread_id, checkpoint_ns, checkpoint_id DESC",
                given,
            ).fetchall()
            return [(row, _writes_of(connection, row)) for row in rows]

        for row, writes in await self._file.read(look):
            found = self._tuple(row, writes)
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
        row = (
            thread_id,
            checkpoint_ns,
            checkpoint["id"],
            config["configurable"].get("checkpoint_id"),
            checkpoint_type,
            checkpoint_blob,
            metadata_type,
            metadata_blob,
        )
        await self._file.write(
            lambda connection: connection.execute(
                f"INSERT INTO langgraph_checkpoints ({_CHECKPOINT_COLUMNS})"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (thread_id, checkpoint_ns, checkpoint_id) DO UPDATE"
                " SET parent_checkpoint_id = excluded.parent_checkpoint_id,"
                "     checkpoint_type = excluded.checkpoint_type,"
                "     checkpoint = excluded.checkpoint,"
                "     metadata_type = excluded.metadata_type, metadata = excluded.metadata",
                row,
            )
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
        rows = []
        for position, (channel, value) in enumerate(writes):
            idx = WRITES_IDX_MAP.get(channel, position)
            value_type, blob = self.serde.dumps_typed(value)
            row = (thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, value_type)
            rows.append((*row, blob, task_path))

        def put(connection: sqlite3.Connection) -> None:
            for row in rows:
                # A write at a position already held is kept; a special one is replaced, as
                # the in-memory saver has it.
                conflict = (
                    "DO UPDATE SET channel = excluded.channel,"
                    " value_type = excluded.value_type,"
                    " value = excluded.value, task_path = excluded.task_path"
                    if row[4] < 0
                    else "DO NOTHING"
                )
                connection.execute(
                    "INSERT INTO langgraph_writes (thread_id, checkpoint_ns, checkpoint_id,"
                    " task_id, idx, channel, value_type, value, task_path)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)"
                    f" {conflict}",
                    row,
                )

        await self._file.write(put)

    async def adelete_thread(self, thread_id: str) -> None:
        await self._file.write(lambda connection: delete_thread(connection, thread_id))

    def get_next_version(self, current: str | None, channel: None) -> str:
        return next_version(current)

    def _tuple(self, row: Row, writes: list[Row]) -> CheckpointTuple:
        thread_id, checkpoint_ns, checkpoint_id, parent = row[:4]
        return CheckpointTuple(
            config={
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": checkpoint_id,
                }
            },
            checkpoint=self.serde.loads_typed((row[4], row[5])),
            metadata=self.serde.loads_typed((row[6], row[7])),
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
                (task, channel, self.serde.loads_typed((value_type, value)))
                for task, channel, value_type, value in writes
            ],
        )


def delete_thread(connection: sqlite3.Connection, thread_id: str) -> None:
    """Every checkpoint of the thread and its writes, in the caller's transaction."""
    connection.execute("DELETE FROM langgraph_writes WHERE thread_id = ?", (thread_id,))
    connection.execute("DELETE FROM langgraph_checkpoints WHERE thread_id = ?", (thread_id,))
