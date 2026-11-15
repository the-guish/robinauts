# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The store on SQLite, in one file, keeping ``schema.sql``'s rendering of the data model.

The standard library's ``sqlite3`` is blocking, so the store keeps the event loop free with one
thread of its own. That thread holds the one connection and runs every operation, one after
the other, each in a transaction: ``BEGIN IMMEDIATE`` for a write, so that it holds the write
lock from its first read, and ``BEGIN`` for a read. Operations therefore never interleave, and
each one decides and writes on the same state, as a transaction with its row locks does on
PostgreSQL. The thread costs the loop nothing while SQLite works, and a desktop's load is far
below what one connection serves.

Every operation checks the owner and ``deleted_at`` itself, as the port promises, and checks a
rule before it writes rather than reading a constraint's violation back: the keys are a second
guard. The backend's process alone opens the file, so watchers are woken in memory. An
operation that commits a change names whom it wakes, and the thread hands that to the loop
after the commit. A cancelled caller cannot stop an operation half way, and the wake comes all
the same.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import sqlite3
import uuid
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from robinauts.controller.adapters.sqlite.schema import BUSY_TIMEOUT, connect, create_or_check
from robinauts.controller.contract.broker.domain import Task, TaskState
from robinauts.controller.contract.domain import TurnActiveError, User
from robinauts.controller.ports.store import (
    Cursor,
    Document,
    Store,
    StoredEvent,
    StoredMessage,
)
from robinauts.shared_domain.domain import (
    Project,
    ProjectNotFoundError,
    Role,
    Session,
    SessionNotFoundError,
    Turn,
    TurnLostError,
    TurnState,
)

_log = logging.getLogger(__name__)

_SESSION_COLUMNS = (
    "id, owner_id, project_id, agent, engine, title, created_at, updated_at, archived_at"
)
_PROJECT_COLUMNS = "id, owner_id, name, created_at"
_TURN_COLUMNS = (
    "id, session_id, follows, model, state, started_at, ended_at, error, retries_message_id,"
    " task_id"
)
_T_COLUMNS = ", ".join("t." + c for c in _TURN_COLUMNS.split(", "))
_TASK_COLUMNS = "id, task_name, payload, state, created_at, lease_until, claimed_at, worker_queue"
_USER_COLUMNS = "id, provider, subject, name, email, created_at, hosted_domain, groups"

_VISIBLE = "SELECT 1 FROM sessions WHERE id = ? AND owner_id = ? AND deleted_at IS NULL"

_HELD = """
SELECT t.task_id FROM turns AS t
JOIN sessions AS s ON s.id = t.session_id
JOIN tasks AS k ON k.id = t.task_id
WHERE t.id = ? AND t.session_id = ? AND s.owner_id = ? AND s.deleted_at IS NULL
  AND t.state = 'active' AND k.state = 'running' AND k.lease_until > ?
"""
"""The turn's task, while the turn is active and its task runs with a lease not passed."""

_ACTIVE_TURN = """
SELECT t.task_id FROM turns AS t
JOIN sessions AS s ON s.id = t.session_id
WHERE t.id = ? AND t.session_id = ? AND s.owner_id = ? AND s.deleted_at IS NULL
  AND t.state = 'active'
"""

_NOT_LIVE = """
  AND NOT EXISTS (SELECT 1 FROM turns AS t JOIN tasks AS k ON k.id = t.task_id
    WHERE t.session_id = s.id AND t.state = 'active' AND k.lease_until > ?)
"""

_CLAIMABLE = "FROM tasks WHERE state = 'queued' AND worker_queue = ? AND lease_until > ?"

Wake = tuple[str, object]
"""Whom a committed change wakes: ``("turn", id)``, ``("queued", worker)``,
``("changes", owner)``, or ``("cancel", task)``, which calls every ``listen_for_cancels``."""


# --- values, as the file holds them ---------------------------------------------


def _at(value: datetime) -> str:
    """UTC text of one width, which sorts as the instant does."""
    if value.tzinfo is None:
        raise ValueError(f"a time without its zone: {value}")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _at_or_none(value: datetime | None) -> str | None:
    return None if value is None else _at(value)


def _time(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _time_or_none(text: str | None) -> datetime | None:
    return None if text is None else _time(text)


def _id(value: uuid.UUID) -> str:
    return str(value)


def _id_or_none(value: uuid.UUID | None) -> str | None:
    return None if value is None else str(value)


def _uuid_or_none(text: str | None) -> uuid.UUID | None:
    return None if text is None else uuid.UUID(text)


def _json(value: Any) -> str:
    return json.dumps(value, allow_nan=False, ensure_ascii=False)


def _session(row: sqlite3.Row) -> Session:
    return Session(
        uuid.UUID(row["id"]),
        uuid.UUID(row["owner_id"]),
        row["agent"],
        row["engine"],
        _time(row["created_at"]),
        _time(row["updated_at"]),
        row["title"],
        project_id=uuid.UUID(row["project_id"]),
        archived_at=_time_or_none(row["archived_at"]),
    )


def _project(row: sqlite3.Row) -> Project:
    return Project(
        uuid.UUID(row["id"]), uuid.UUID(row["owner_id"]), row["name"], _time(row["created_at"])
    )


def _turn(row: sqlite3.Row) -> Turn:
    return Turn(
        uuid.UUID(row["id"]),
        uuid.UUID(row["session_id"]),
        uuid.UUID(row["follows"]),
        row["model"],
        TurnState(row["state"]),
        _time(row["started_at"]),
        uuid.UUID(row["task_id"]),
        _time_or_none(row["ended_at"]),
        row["error"],
        _uuid_or_none(row["retries_message_id"]),
    )


def _task(row: sqlite3.Row) -> Task:
    return Task(
        uuid.UUID(row["id"]),
        row["task_name"],
        json.loads(row["payload"]),
        TaskState(row["state"]),
        _time(row["created_at"]),
        _time(row["lease_until"]),
        _time_or_none(row["claimed_at"]),
        row["worker_queue"],
    )


def _user(row: sqlite3.Row) -> User:
    return User(
        uuid.UUID(row["id"]),
        row["provider"],
        row["subject"],
        row["name"],
        row["email"],
        _time(row["created_at"]),
        row["hosted_domain"],
        tuple(json.loads(row["groups"])),
    )


# --- what runs on the store's thread ---------------------------------------------


class _Transaction:
    """One operation's connection, and whom its commit wakes."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.wakes: list[Wake] = []

    def one(self, sql: str, *parameters: object) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.connection.execute(sql, parameters).fetchone()
        return row

    def all(self, sql: str, *parameters: object) -> list[sqlite3.Row]:
        return self.connection.execute(sql, parameters).fetchall()

    def run(self, sql: str, *parameters: object) -> int:
        """The statement, and the rows it changed."""
        return self.connection.execute(sql, parameters).rowcount

    def visible(self, owner: uuid.UUID, session: uuid.UUID) -> None:
        if self.one(_VISIBLE, _id(session), _id(owner)) is None:
            raise SessionNotFoundError(str(session))

    def bump(self, owner: uuid.UUID) -> None:
        """The owner's change version moved by one, and its waiters woken on commit."""
        if self.run(
            "UPDATE users SET change_version = change_version + 1 WHERE id = ?", _id(owner)
        ):
            self.wakes.append(("changes", owner))

    def insert_message(self, message: StoredMessage) -> None:
        role = message.role.value if isinstance(message.role, Role) else message.role
        self.run(
            "INSERT INTO messages (id, session_id, parent_id, role, created_at, document)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            _id(message.id),
            _id(message.session_id),
            _id_or_none(message.parent_id),
            role,
            _at(message.created_at),
            _json(message.document),
        )


class SQLiteStore(Store):
    """Over the file at ``path``, which ``open`` makes, or checks, and ``close`` lets go."""

    def __init__(self, path: Path, *, busy_timeout: float = BUSY_TIMEOUT) -> None:
        self._path = path
        self._busy_timeout = busy_timeout
        self._connection: sqlite3.Connection | None = None
        self._thread: ThreadPoolExecutor | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._waiters: dict[Wake, set[asyncio.Future[None]]] = {}
        self._stops: list[Callable[[uuid.UUID], object]] = []

    @property
    def path(self) -> Path:
        return self._path

    async def open(self) -> None:
        """Open the file and apply or check its schema. Nothing for the engines: their state is
        not in this file."""
        if self._thread is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._thread = ThreadPoolExecutor(max_workers=1, thread_name_prefix="robinauts-sqlite")
        try:
            await self._loop.run_in_executor(self._thread, self._connect)
        except BaseException:
            thread, self._thread = self._thread, None
            thread.shutdown(wait=True)
            raise

    async def close(self) -> None:
        if self._thread is None:
            return
        thread, self._thread = self._thread, None
        try:
            await asyncio.get_running_loop().run_in_executor(thread, self._disconnect)
        finally:
            thread.shutdown(wait=True)

    def _connect(self) -> None:
        connection = connect(self._path, busy_timeout=self._busy_timeout)
        try:
            create_or_check(connection, self._path)
        except BaseException:
            connection.close()
            raise
        connection.row_factory = sqlite3.Row
        self._connection = connection

    def _disconnect(self) -> None:
        if self._connection is not None:
            connection, self._connection = self._connection, None
            connection.close()

    # --- running an operation ---------------------------------------------------

    async def _write[T](self, work: Callable[[_Transaction], T]) -> T:
        return await self._on_thread(work, "BEGIN IMMEDIATE")

    async def _read[T](self, work: Callable[[_Transaction], T]) -> T:
        return await self._on_thread(work, "BEGIN")

    async def _on_thread[T](self, work: Callable[[_Transaction], T], begin: str) -> T:
        if self._thread is None:
            raise RuntimeError("the store is not open")
        return await asyncio.get_running_loop().run_in_executor(
            self._thread, self._transaction, work, begin
        )

    def _transaction[T](self, work: Callable[[_Transaction], T], begin: str) -> T:
        connection = self._connection
        if connection is None:
            raise RuntimeError("the store is not open")
        transaction = _Transaction(connection)
        try:
            connection.execute(begin)
            try:
                result = work(transaction)
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        except sqlite3.OperationalError as failed:
            if getattr(failed, "sqlite_errorname", "") in ("SQLITE_BUSY", "SQLITE_LOCKED"):
                raise TimeoutError(
                    f"the database file was locked by another process for {self._busy_timeout} s"
                ) from failed
            raise
        if transaction.wakes and self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._wake, transaction.wakes)
            except RuntimeError:
                pass  # The loop is closed: nobody is left to wake.
        return result

    def _wake(self, wakes: list[Wake]) -> None:
        for wake in wakes:
            kind, value = wake
            if kind == "cancel":
                assert isinstance(value, uuid.UUID)
                for stop in self._stops:
                    stop(value)
                continue
            for woken in self._waiters.get(wake, ()):
                if not woken.done():
                    woken.set_result(None)

    async def _wait_until[T](
        self,
        wake: Wake,
        timeout: float,
        read: Callable[[], Any],
        done: Callable[[T], bool],
    ) -> tuple[bool, T]:
        """Read until ``done`` holds, woken by ``wake``, or ``timeout`` passes: whether it held,
        and the last read. The waiter is registered before each read, so a commit between the
        read and the sleep wakes it."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            woken: asyncio.Future[None] = loop.create_future()
            waiting = self._waiters.setdefault(wake, set())
            waiting.add(woken)
            try:
                value: T = await read()
                if done(value):
                    return True, value
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return False, value
                try:
                    await asyncio.wait_for(asyncio.shield(woken), remaining)
                except TimeoutError:
                    value = await read()
                    return done(value), value
            finally:
                waiting.discard(woken)
                if not waiting and self._waiters.get(wake) is waiting:
                    del self._waiters[wake]

    # --- users --------------------------------------------------------------

    async def add_or_refresh_user(self, user: User) -> User:
        if user.created_at is None:
            raise ValueError("a user is stored with the time it was first seen")
        created_at = user.created_at

        def work(tx: _Transaction) -> User:
            tx.run(
                f"INSERT INTO users ({_USER_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (provider, subject) DO UPDATE SET"
                " name = excluded.name, email = excluded.email,"
                " hosted_domain = excluded.hosted_domain, groups = excluded.groups",
                _id(user.id),
                user.provider,
                user.subject,
                user.name,
                user.email,
                _at(created_at),
                user.hosted_domain,
                _json(list(user.groups)),
            )
            row = tx.one(
                f"SELECT {_USER_COLUMNS} FROM users WHERE provider = ? AND subject = ?",
                user.provider,
                user.subject,
            )
            assert row is not None
            return _user(row)

        return await self._write(work)

    # --- projects -----------------------------------------------------------

    async def ensure_project(self, project: Project) -> Project:
        def work(tx: _Transaction) -> Project:
            stored = tx.run(
                f"INSERT INTO projects ({_PROJECT_COLUMNS}) VALUES (?, ?, ?, ?)"
                " ON CONFLICT (owner_id, name) DO NOTHING",
                _id(project.id),
                _id(project.owner_id),
                project.name,
                _at(project.created_at),
            )
            if stored:
                tx.bump(project.owner_id)
                return project
            there = tx.one(
                f"SELECT {_PROJECT_COLUMNS} FROM projects WHERE owner_id = ? AND name = ?",
                _id(project.owner_id),
                project.name,
            )
            assert there is not None
            return _project(there)

        return await self._write(work)

    async def get_project(self, owner: uuid.UUID, project: uuid.UUID) -> Project:
        row = await self._read(
            lambda tx: tx.one(
                f"SELECT {_PROJECT_COLUMNS} FROM projects WHERE id = ? AND owner_id = ?",
                _id(project),
                _id(owner),
            )
        )
        if row is None:
            raise ProjectNotFoundError(str(project))
        return _project(row)

    async def projects_of(self, owner: uuid.UUID) -> list[Project]:
        rows = await self._read(
            lambda tx: tx.all(
                f"SELECT {_PROJECT_COLUMNS} FROM projects WHERE owner_id = ?"
                " ORDER BY created_at, id",
                _id(owner),
            )
        )
        return [_project(row) for row in rows]

    # --- sessions -----------------------------------------------------------

    async def add_session(self, session: Session) -> None:
        def work(tx: _Transaction) -> None:
            if (
                tx.one(
                    "SELECT 1 FROM projects WHERE id = ? AND owner_id = ?",
                    _id(session.project_id),
                    _id(session.owner_id),
                )
                is None
            ):
                raise ProjectNotFoundError(str(session.project_id))
            tx.run(
                f"INSERT INTO sessions ({_SESSION_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _id(session.id),
                _id(session.owner_id),
                _id(session.project_id),
                session.agent,
                session.engine,
                session.title,
                _at(session.created_at),
                _at(session.updated_at),
                _at_or_none(session.archived_at),
            )
            tx.bump(session.owner_id)

        await self._write(work)

    async def get_session(self, owner: uuid.UUID, session: uuid.UUID) -> Session:
        row = await self._read(
            lambda tx: tx.one(
                f"SELECT {_SESSION_COLUMNS} FROM sessions"
                " WHERE id = ? AND owner_id = ? AND deleted_at IS NULL",
                _id(session),
                _id(owner),
            )
        )
        if row is None:
            raise SessionNotFoundError(str(session))
        return _session(row)

    async def update_session(self, session: Session) -> None:
        def work(tx: _Transaction) -> None:
            if tx.run(
                "UPDATE sessions SET title = ?, updated_at = ?, archived_at = ?"
                " WHERE id = ? AND owner_id = ?",
                session.title,
                _at(session.updated_at),
                _at_or_none(session.archived_at),
                _id(session.id),
                _id(session.owner_id),
            ):
                tx.bump(session.owner_id)

        await self._write(work)

    async def sessions_of(
        self, owner: uuid.UUID, limit: int, before: Cursor | None
    ) -> list[Session]:
        updated_at, session_id = before if before is not None else (None, None)
        rows = await self._read(
            lambda tx: tx.all(
                f"SELECT {_SESSION_COLUMNS} FROM sessions"
                " WHERE owner_id = ? AND deleted_at IS NULL AND archived_at IS NULL"
                "   AND (? IS NULL OR (updated_at, id) < (?, ?))"
                " ORDER BY updated_at DESC, id DESC LIMIT ?",
                _id(owner),
                _at_or_none(updated_at),
                _at_or_none(updated_at),
                _id_or_none(session_id),
                limit,
            )
        )
        return [_session(row) for row in rows]

    async def hide_session(self, owner: uuid.UUID, session: uuid.UUID, at: datetime) -> None:
        def work(tx: _Transaction) -> None:
            if not tx.run(
                "UPDATE sessions SET deleted_at = ?"
                " WHERE id = ? AND owner_id = ? AND deleted_at IS NULL",
                _at(at),
                _id(session),
                _id(owner),
            ):
                raise SessionNotFoundError(str(session))
            tx.bump(owner)

        await self._write(work)

    async def purge_session(self, owner: uuid.UUID, session: uuid.UUID, now: datetime) -> bool:
        def work(tx: _Transaction) -> bool:
            tasks = [
                row["task_id"]
                for row in tx.all("SELECT task_id FROM turns WHERE session_id = ?", _id(session))
            ]
            if not tx.run(
                "DELETE FROM sessions AS s WHERE id = ? AND owner_id = ?" + _NOT_LIVE,
                _id(session),
                _id(owner),
                _at(now),
            ):
                return False
            # The turns went with the session; their tasks are no one's now.
            for task in tasks:
                tx.run("DELETE FROM tasks WHERE id = ?", task)
            return True

        return await self._write(work)

    async def messages_of(self, owner: uuid.UUID, session: uuid.UUID) -> list[Document]:
        def work(tx: _Transaction) -> list[sqlite3.Row]:
            tx.visible(owner, session)
            return tx.all(
                "SELECT document FROM messages WHERE session_id = ? ORDER BY created_at, id",
                _id(session),
            )

        return [json.loads(row["document"]) for row in await self._read(work)]

    # --- turns --------------------------------------------------------------

    async def queue_turn(
        self, owner: uuid.UUID, turn: Turn, task: Task, question: StoredMessage | None
    ) -> None:
        def work(tx: _Transaction) -> None:
            tx.visible(owner, turn.session_id)
            if (
                tx.one(
                    "SELECT 1 FROM turns WHERE session_id = ? AND state = 'active'",
                    _id(turn.session_id),
                )
                is not None
            ):
                raise TurnActiveError(str(turn.session_id))
            if question is not None:
                tx.insert_message(question)
            # The task first: the turn refers to it.
            tx.run(
                f"INSERT INTO tasks ({_TASK_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                _id(task.id),
                task.name,
                _json(task.payload),
                task.state.value,
                _at(task.created_at),
                _at(task.lease_until),
                _at_or_none(task.claimed_at),
                task.worker,
            )
            tx.run(
                f"INSERT INTO turns ({_TURN_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _id(turn.id),
                _id(turn.session_id),
                _id(turn.follows),
                turn.model,
                turn.state.value,
                _at(turn.started_at),
                _at_or_none(turn.ended_at),
                turn.error,
                _id_or_none(turn.retries_message_id),
                _id(turn.task_id),
            )
            if task.state is TaskState.QUEUED:
                tx.wakes.append(("queued", task.worker))
            tx.bump(owner)

        await self._write(work)

    async def append_event(
        self,
        owner: uuid.UUID,
        session: uuid.UUID,
        turn: uuid.UUID,
        position: int,
        document: Document,
        written_at: datetime,
        expires_at: datetime,
    ) -> None:
        def work(tx: _Transaction) -> None:
            if tx.one(_HELD, _id(turn), _id(session), _id(owner), _at(written_at)) is None:
                raise TurnLostError(f"turn {turn} is not running")
            held = tx.one(
                "SELECT document FROM turn_events WHERE turn_id = ? AND position = ?",
                _id(turn),
                position,
            )
            if held is not None:
                # The same document is the runner's own write, acknowledged late; another is
                # a second runner.
                if json.loads(held["document"]) != document:
                    raise TurnLostError(f"position {position} of turn {turn} holds another event")
                return
            tx.run(
                "INSERT INTO turn_events (turn_id, position, document, expires_at)"
                " VALUES (?, ?, ?, ?)",
                _id(turn),
                position,
                _json(document),
                _at(expires_at),
            )
            tx.wakes.append(("turn", turn))

        await self._write(work)

    async def events_after(
        self, owner: uuid.UUID, session: uuid.UUID, turn: uuid.UUID, position: int
    ) -> list[tuple[int, Document]]:
        def work(tx: _Transaction) -> list[sqlite3.Row]:
            tx.visible(owner, session)
            return tx.all(
                "SELECT e.position, e.document FROM turn_events AS e"
                " JOIN turns AS t ON t.id = e.turn_id"
                " WHERE t.id = ? AND t.session_id = ? AND e.position > ?"
                " ORDER BY e.position",
                _id(turn),
                _id(session),
                position,
            )

        return [(row["position"], json.loads(row["document"])) for row in await self._read(work)]

    async def finish_turn(
        self,
        owner: uuid.UUID,
        session: uuid.UUID,
        turn: uuid.UUID,
        state: TurnState,
        ended_at: datetime,
        error: str | None,
        answer: StoredMessage | None,
        events: Sequence[StoredEvent],
        updated_at: datetime,
    ) -> None:
        def work(tx: _Transaction) -> None:
            if not tx.run(
                "UPDATE sessions SET updated_at = ?"
                " WHERE id = ? AND owner_id = ? AND deleted_at IS NULL",
                _at(updated_at),
                _id(session),
                _id(owner),
            ):
                raise SessionNotFoundError(str(session))
            held = tx.one(_HELD, _id(turn), _id(session), _id(owner), _at(ended_at))
            if held is None:
                raise TurnLostError(f"turn {turn} is not running")
            finished_already = TurnLostError(f"turn {turn} was finished already")
            if answer is not None and (
                tx.one("SELECT 1 FROM messages WHERE id = ?", _id(answer.id)) is not None
            ):
                raise finished_already
            tx.run(
                "UPDATE turns SET state = ?, ended_at = ?, error = ? WHERE id = ?",
                state.value,
                _at(ended_at),
                error,
                _id(turn),
            )
            tx.run("UPDATE tasks SET state = 'done' WHERE id = ?", held["task_id"])
            if answer is not None:
                tx.insert_message(answer)
            for event in events:
                if (
                    tx.one(
                        "SELECT 1 FROM turn_events WHERE turn_id = ? AND position = ?",
                        _id(turn),
                        event.position,
                    )
                    is not None
                ):
                    raise finished_already
                tx.run(
                    "INSERT INTO turn_events (turn_id, position, document, expires_at)"
                    " VALUES (?, ?, ?, ?)",
                    _id(turn),
                    event.position,
                    _json(event.document),
                    _at(event.expires_at),
                )
            tx.wakes.append(("turn", turn))
            tx.bump(owner)

        await self._write(work)

    async def end_expired_turn(
        self,
        owner: uuid.UUID,
        session: uuid.UUID,
        turn: uuid.UUID,
        now: datetime,
        answer: StoredMessage | None = None,
    ) -> Turn | None:
        def work(tx: _Transaction) -> Turn | None:
            active = tx.one(_ACTIVE_TURN, _id(turn), _id(session), _id(owner))
            if active is None:
                return None
            task = tx.one(
                "SELECT state FROM tasks WHERE id = ? AND state <> 'done' AND lease_until < ?",
                active["task_id"],
                _at(now),
            )
            if task is None:
                return None
            queued = task["state"] == TaskState.QUEUED.value
            error = "queued too long" if queued else "lease expired"
            tx.run(
                "UPDATE turns SET state = 'interrupted', ended_at = ?, error = ? WHERE id = ?",
                _at(now),
                error,
                _id(turn),
            )
            tx.run("UPDATE tasks SET state = 'done' WHERE id = ?", active["task_id"])
            if answer is not None:
                tx.insert_message(answer)
            row = tx.one(f"SELECT {_TURN_COLUMNS} FROM turns WHERE id = ?", _id(turn))
            assert row is not None
            tx.wakes.append(("turn", turn))
            tx.bump(owner)
            return _turn(row)

        return await self._write(work)

    async def renew_leases(
        self, tasks: Sequence[uuid.UUID], now: datetime, until: datetime
    ) -> list[uuid.UUID]:
        def work(tx: _Transaction) -> list[uuid.UUID]:
            stopping = []
            for task in tasks:
                row = tx.one(
                    "SELECT cancel_requested_at FROM tasks"
                    " WHERE id = ? AND state = 'running' AND lease_until > ?",
                    _id(task),
                    _at(now),
                )
                if row is None:
                    continue
                tx.run("UPDATE tasks SET lease_until = ? WHERE id = ?", _at(until), _id(task))
                if row["cancel_requested_at"] is not None:
                    stopping.append(task)
            return stopping

        return await self._write(work)

    async def claim_tasks(
        self, now: datetime, until: datetime, limit: int, worker: str
    ) -> list[Task]:
        def work(tx: _Transaction) -> list[Task]:
            claimed = []
            for row in tx.all(
                f"SELECT {_TASK_COLUMNS} {_CLAIMABLE} ORDER BY created_at, id LIMIT ?",
                worker,
                _at(now),
                limit,
            ):
                tx.run(
                    "UPDATE tasks SET state = 'running', lease_until = ?, claimed_at = ?"
                    " WHERE id = ?",
                    _at(until),
                    _at(now),
                    row["id"],
                )
                claimed.append(
                    dataclasses.replace(
                        _task(row), state=TaskState.RUNNING, lease_until=until, claimed_at=now
                    )
                )
            return claimed

        return await self._write(work)

    async def wait_for_queued(self, now: datetime, timeout: float, worker: str) -> bool:
        async def claimable() -> bool:
            return await self._read(
                lambda tx: tx.one(f"SELECT 1 {_CLAIMABLE}", worker, _at(now)) is not None
            )

        found, _ = await self._wait_until(("queued", worker), timeout, claimable, bool)
        return found

    async def request_cancel(
        self, owner: uuid.UUID, session: uuid.UUID, turn: uuid.UUID, at: datetime
    ) -> None:
        def work(tx: _Transaction) -> None:
            active = tx.one(_ACTIVE_TURN, _id(turn), _id(session), _id(owner))
            if active is None:
                return
            task = active["task_id"]
            row = tx.one("SELECT state FROM tasks WHERE id = ? AND state <> 'done'", task)
            if row is None:
                return
            if row["state"] == TaskState.QUEUED.value:
                # No worker holds it: both end here.
                tx.run(
                    "UPDATE tasks SET state = 'done', cancel_requested_at = ? WHERE id = ?",
                    _at(at),
                    task,
                )
                tx.run(
                    "UPDATE turns SET state = 'cancelled', ended_at = ? WHERE id = ?",
                    _at(at),
                    _id(turn),
                )
                tx.wakes.append(("turn", turn))
                tx.bump(owner)
            elif row["state"] == TaskState.RUNNING.value:
                tx.run("UPDATE tasks SET cancel_requested_at = ? WHERE id = ?", _at(at), task)
                tx.wakes.append(("cancel", uuid.UUID(task)))

        await self._write(work)

    async def listen_for_cancels(self, stop: Callable[[uuid.UUID], object]) -> None:
        self._stops.append(stop)

    async def hidden_sessions(self, now: datetime) -> list[Session]:
        rows = await self._read(
            lambda tx: tx.all(
                f"SELECT {_SESSION_COLUMNS} FROM sessions AS s"
                " WHERE deleted_at IS NOT NULL" + _NOT_LIVE,
                _at(now),
            )
        )
        return [_session(row) for row in rows]

    async def expired_tasks(self, now: datetime) -> list[Task]:
        rows = await self._read(
            lambda tx: tx.all(
                f"SELECT {_TASK_COLUMNS} FROM tasks WHERE state <> 'done' AND lease_until < ?",
                _at(now),
            )
        )
        return [_task(row) for row in rows]

    async def end_expired_task(self, task: uuid.UUID, now: datetime) -> bool:
        changed = await self._write(
            lambda tx: tx.run(
                "UPDATE tasks SET state = 'done'"
                " WHERE id = ? AND state <> 'done' AND lease_until < ?",
                _id(task),
                _at(now),
            )
        )
        return changed > 0

    async def active_turn(self, owner: uuid.UUID, session: uuid.UUID) -> Turn | None:
        def work(tx: _Transaction) -> sqlite3.Row | None:
            tx.visible(owner, session)
            return tx.one(
                f"SELECT {_TURN_COLUMNS} FROM turns WHERE session_id = ? AND state = 'active'",
                _id(session),
            )

        row = await self._read(work)
        return None if row is None else _turn(row)

    async def get_task(self, task: uuid.UUID) -> Task | None:
        row = await self._read(
            lambda tx: tx.one(f"SELECT {_TASK_COLUMNS} FROM tasks WHERE id = ?", _id(task))
        )
        return None if row is None else _task(row)

    async def find_turn(self, turn: uuid.UUID) -> tuple[uuid.UUID, Turn] | None:
        row = await self._read(
            lambda tx: tx.one(
                f"SELECT s.owner_id, {_T_COLUMNS} FROM turns AS t"
                " JOIN sessions AS s ON s.id = t.session_id"
                " WHERE t.id = ? AND s.deleted_at IS NULL",
                _id(turn),
            )
        )
        return None if row is None else (uuid.UUID(row["owner_id"]), _turn(row))

    async def latest_turn(self, owner: uuid.UUID, session: uuid.UUID) -> Turn | None:
        def work(tx: _Transaction) -> sqlite3.Row | None:
            tx.visible(owner, session)
            return tx.one(
                f"SELECT {_TURN_COLUMNS} FROM turns WHERE session_id = ?"
                " ORDER BY started_at DESC, id DESC LIMIT 1",
                _id(session),
            )

        row = await self._read(work)
        return None if row is None else _turn(row)

    async def get_turn(self, owner: uuid.UUID, session: uuid.UUID, turn: uuid.UUID) -> Turn | None:
        def work(tx: _Transaction) -> sqlite3.Row | None:
            tx.visible(owner, session)
            return tx.one(
                f"SELECT {_TURN_COLUMNS} FROM turns WHERE id = ? AND session_id = ?",
                _id(turn),
                _id(session),
            )

        row = await self._read(work)
        return None if row is None else _turn(row)

    async def wait_for_events(
        self, owner: uuid.UUID, session: uuid.UUID, turn: uuid.UUID, after: int, timeout: float
    ) -> bool:
        def ready(tx: _Transaction) -> bool:
            row = tx.one(
                "SELECT t.state, EXISTS (SELECT 1 FROM turn_events AS e"
                "   WHERE e.turn_id = t.id AND e.position > ?) AS more"
                " FROM turns AS t JOIN sessions AS s ON s.id = t.session_id"
                " WHERE t.id = ? AND t.session_id = ? AND s.owner_id = ?",
                after,
                _id(turn),
                _id(session),
                _id(owner),
            )
            return row is None or bool(row["more"]) or row["state"] != TurnState.ACTIVE.value

        async def read() -> bool:
            return await self._read(ready)

        found, _ = await self._wait_until(("turn", turn), timeout, read, bool)
        return found

    # --- changes ------------------------------------------------------------

    async def running_sessions(
        self, owner: uuid.UUID, sessions: Sequence[uuid.UUID]
    ) -> frozenset[uuid.UUID]:
        asked = [_id(s) for s in sessions]
        if not asked:
            return frozenset()
        marks = ", ".join("?" * len(asked))
        rows = await self._read(
            lambda tx: tx.all(
                "SELECT t.session_id FROM turns AS t JOIN sessions AS s ON s.id = t.session_id"
                f" WHERE t.session_id IN ({marks}) AND t.state = 'active'"
                "   AND s.owner_id = ? AND s.deleted_at IS NULL",
                *asked,
                _id(owner),
            )
        )
        return frozenset(uuid.UUID(row["session_id"]) for row in rows)

    async def wait_for_changes(self, owner: uuid.UUID, after: int | None, timeout: float) -> int:
        async def version() -> int:
            row = await self._read(
                lambda tx: tx.one("SELECT change_version FROM users WHERE id = ?", _id(owner))
            )
            return 0 if row is None else int(row["change_version"])

        if after is None:
            return await version()
        _, found = await self._wait_until(
            ("changes", owner), timeout, version, lambda v: v != after
        )
        return found
