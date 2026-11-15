# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The SQLite store keeps the store's contract, on a file of its own, and keeps what it wrote
once the file is opened again."""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest

from robinauts.controller.adapters.sqlite.schema import FILE_NAME
from robinauts.controller.adapters.sqlite.store import SQLiteStore
from robinauts.controller.ports.store import Store
from robinauts.shared_domain.domain import TurnState
from util.aio import asyncio_test
from util.contracts.store import NOW, StoreContract, add_user, piece, question, session, turn, user

pytestmark = pytest.mark.io


class TestSQLiteStore(StoreContract):
    async def new_store(self) -> Store:
        folder = Path(tempfile.mkdtemp(prefix="robinauts-sqlite-"))
        store = SQLiteStore(folder / FILE_NAME)
        await store.open()
        return store

    async def close_store(self, store: Store) -> None:
        assert isinstance(store, SQLiteStore)
        await store.close()
        shutil.rmtree(store.path.parent)


@asyncio_test
async def test_what_a_turn_wrote_is_read_back_after_the_file_is_opened_again(
    tmp_path: Path,
) -> None:
    path = tmp_path / "data" / FILE_NAME
    first = SQLiteStore(path)
    await first.open()
    me = await add_user(first, user())
    one = session(me.id)
    await first.add_session(one)
    asked = question(one.id)
    running, task = turn(one.id, asked.id)
    await first.queue_turn(me.id, running, task, asked)
    await first.append_event(me.id, one.id, running.id, 1, piece(1).document, NOW, NOW)
    await first.close()

    again = SQLiteStore(path)
    await again.open()
    try:
        assert await again.get_session(me.id, one.id) == one
        assert await again.messages_of(me.id, one.id) == [asked.document]
        assert await again.active_turn(me.id, one.id) == running
        assert await again.events_after(me.id, one.id, running.id, 0) == [(1, piece(1).document)]
        assert await again.wait_for_changes(me.id, None, 0) == 3
    finally:
        await again.close()
    # Readable by this user alone, folder and file.
    assert path.stat().st_mode & 0o077 == 0
    assert path.parent.stat().st_mode & 0o077 == 0


@asyncio_test
async def test_a_write_on_a_file_another_process_holds_times_out_instead_of_hanging(
    tmp_path: Path,
) -> None:
    path = tmp_path / FILE_NAME
    store = SQLiteStore(path, busy_timeout=0.3)
    await store.open()
    me = await add_user(store, user())
    one = session(me.id)
    await store.add_session(one)
    asked = question(one.id)
    running, task = turn(one.id, asked.id)
    await store.queue_turn(me.id, running, task, asked)
    holder = sqlite3.connect(path, isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        async with asyncio.timeout(10):
            with pytest.raises(TimeoutError):
                await store.finish_turn(
                    me.id, one.id, running.id, TurnState.FINISHED, NOW, None, None, [], NOW
                )
        # A reader is not held back by the writer.
        assert await store.active_turn(me.id, one.id) == running
        holder.execute("ROLLBACK")
        await store.finish_turn(
            me.id, one.id, running.id, TurnState.FINISHED, NOW, None, None, [], NOW
        )
    finally:
        holder.close()
        await store.close()
