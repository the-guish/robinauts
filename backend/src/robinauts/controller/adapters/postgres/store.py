# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The store over asyncpg, keeping ``schema.sql``'s rendering of the data model.

Every operation checks the owner and ``deleted_at`` itself, as the port promises. The
session row is locked first in every transaction that writes a session and one of its
turns: ``start_turn`` with ``FOR SHARE``, ``finish_turn`` and ``hide_session`` with an
``UPDATE`` or a ``FOR NO KEY UPDATE``, so that no two of them deadlock. Constraint names
are the interface with the database: a violation is translated by name. Watchers are
woken by ``NOTIFY`` on one listening connection per store, held apart from the pool, and
read the store again in every case, so a notification lost with a dropped connection
costs a timeout and nothing else.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import datetime, timedelta
from typing import TypeVar

import asyncpg

from robinauts.controller.adapters.postgres.pool import open_pool
from robinauts.controller.adapters.postgres.schema import check_schema
from robinauts.controller.contract.domain import (
    Role,
    Session,
    SessionNotFoundError,
    Turn,
    TurnActiveError,
    TurnLostError,
    TurnState,
    User,
)
from robinauts.controller.ports.store import (
    Cursor,
    Document,
    Store,
    StoredEvent,
    StoredMessage,
)
from robinauts.controller.ports.work import Fence, Held, Renewed, WorkQueue

T = TypeVar("T")

log = logging.getLogger(__name__)

CHANNEL = "robinauts_turns"
"""Where a turn's writes are announced: ``<turn> <position>``, or ``<turn> end``."""

CANCEL_CHANNEL = "robinauts_cancel"
"""Where a cancel is asked for: ``<turn>``, which its holder, wherever it runs, stops."""

LISTENER_CHECK = 5.0
"""How often a reader of cancel signals makes sure the listening connection is up."""

ONE_RUNNING = "turns_one_running_per_session"
POSITION_TAKEN = "turn_events_pkey"
USER_EXISTS = "users_provider_subject_key"

_SESSION_COLUMNS = "id, owner_id, agent, engine, title, created_at, updated_at"
_TURN_COLUMNS = (
    "id, session_id, follows, model, state, started_at, ended_at, error, lease_until, retries,"
    " deadline_at, worker_id, attempt, heartbeat_at, cancel_requested_at"
)

_VISIBLE = "SELECT 1 FROM sessions WHERE id = $1 AND owner_id = $2 AND deleted_at IS NULL"

_HELD = """
SELECT t.id
FROM turns AS t
JOIN sessions AS s ON s.id = t.session_id
WHERE t.id = $3 AND t.session_id = $2 AND s.owner_id = $1 AND s.deleted_at IS NULL
  AND t.state = 'running' AND t.lease_until > $4 AND t.worker_id = $5 AND t.attempt = $6
FOR SHARE OF t
"""

_HEARTBEAT = """
UPDATE turns AS t SET lease_until = $3, heartbeat_at = $2
FROM unnest($4::uuid[], $5::integer[]) AS h(id, attempt)
WHERE t.id = h.id AND t.attempt = h.attempt AND t.worker_id = $1
  AND t.state = 'running' AND t.lease_until > $2
RETURNING t.id, t.cancel_requested_at
"""

_END_EXPIRED = f"""
UPDATE turns SET state = $3, ended_at = $4, error = $5
WHERE id = $1 AND session_id = $2 AND state = 'running' AND lease_until < $4
RETURNING {_TURN_COLUMNS}
"""


def _session(row: asyncpg.Record) -> Session:
    return Session(
        row["id"],
        row["owner_id"],
        row["agent"],
        row["engine"],
        row["created_at"],
        row["updated_at"],
        row["title"],
    )


def _turn(row: asyncpg.Record) -> Turn:
    return Turn(
        row["id"],
        row["session_id"],
        row["follows"],
        row["model"],
        TurnState(row["state"]),
        row["started_at"],
        row["lease_until"],
        row["ended_at"],
        row["error"],
        row["retries"],
        row["deadline_at"],
        row["worker_id"],
        row["attempt"],
        row["heartbeat_at"],
        row["cancel_requested_at"],
    )


def _user(row: asyncpg.Record) -> User:
    return User(
        row["id"], row["provider"], row["subject"], row["name"], row["email"], row["created_at"]
    )


def _rows(status: str) -> int:
    """The row count at the end of a command tag such as ``UPDATE 1``."""
    return int(status.rsplit(" ", 1)[-1])


class PostgresStore(Store, WorkQueue):
    """Over a pool it is given, which the giver closes, or over a dsn, from which ``open``
    makes its own pool, checks the schema, and ``close`` closes it."""

    def __init__(self, pool: asyncpg.Pool | None = None, dsn: str | None = None) -> None:
        self._pool: asyncpg.Pool = pool  # type: ignore[assignment]
        self._owns_pool = pool is None
        self._dsn = dsn
        self._listener: asyncpg.Connection | None = None
        self._waiters: dict[uuid.UUID, set[asyncio.Future[None]]] = {}
        self._opening: asyncio.Lock | None = None
        self._work: asyncpg.Connection | None = None
        self._cancels: set[asyncio.Queue[uuid.UUID]] = set()
        self._work_lock: asyncio.Lock | None = None

    @property
    def pool(self) -> asyncpg.Pool:
        """The pool given, or the one ``open`` made: the credentials share it."""
        return self._pool

    async def open(self) -> asyncpg.Pool:
        if self._owns_pool and self._pool is None:
            if self._dsn is None:
                raise RuntimeError("a store opened from nothing needs the database's dsn")
            self._pool = await open_pool(self._dsn)
        await check_schema(self._pool)
        return self._pool

    async def close(self) -> None:
        if self._work is not None:
            work, self._work = self._work, None
            await work.close()
        if self._listener is not None:
            listener, self._listener = self._listener, None
            await listener.close()
        if self._owns_pool and self._pool is not None:
            pool, self._pool = self._pool, None  # type: ignore[assignment]
            await pool.close()

    # --- users --------------------------------------------------------------

    async def add_user_if_absent(self, user: User) -> User:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                "INSERT INTO users (id, provider, subject, name, email, created_at)"
                " VALUES ($1, $2, $3, $4, $5, $6)"
                f" ON CONFLICT ON CONSTRAINT {USER_EXISTS} DO NOTHING"
                " RETURNING id, provider, subject, name, email, created_at",
                user.id,
                user.provider,
                user.subject,
                user.name,
                user.email,
                user.created_at,
            )
            if row is None:
                row = await connection.fetchrow(
                    "SELECT id, provider, subject, name, email, created_at FROM users"
                    " WHERE provider = $1 AND subject = $2",
                    user.provider,
                    user.subject,
                )
            return _user(row)

    # --- sessions -----------------------------------------------------------

    async def add_session(self, session: Session) -> None:
        await self._pool.execute(
            f"INSERT INTO sessions ({_SESSION_COLUMNS}) VALUES ($1, $2, $3, $4, $5, $6, $7)",
            session.id,
            session.owner_id,
            session.agent,
            session.engine,
            session.title,
            session.created_at,
            session.updated_at,
        )

    async def get_session(self, owner: uuid.UUID, session: uuid.UUID) -> Session:
        row = await self._pool.fetchrow(
            f"SELECT {_SESSION_COLUMNS} FROM sessions"
            " WHERE id = $1 AND owner_id = $2 AND deleted_at IS NULL",
            session,
            owner,
        )
        if row is None:
            raise SessionNotFoundError(str(session))
        return _session(row)

    async def update_session(self, session: Session) -> None:
        await self._pool.execute(
            "UPDATE sessions SET title = $2, updated_at = $3 WHERE id = $1",
            session.id,
            session.title,
            session.updated_at,
        )

    async def sessions_of(
        self, owner: uuid.UUID, limit: int, before: Cursor | None
    ) -> list[Session]:
        updated_at, session_id = before if before is not None else (None, None)
        rows = await self._pool.fetch(
            f"SELECT {_SESSION_COLUMNS} FROM sessions"
            " WHERE owner_id = $1 AND deleted_at IS NULL"
            "   AND ($2::timestamptz IS NULL OR (updated_at, id) < ($2, $3::uuid))"
            " ORDER BY updated_at DESC, id DESC LIMIT $4",
            owner,
            updated_at,
            session_id,
            limit,
        )
        return [_session(row) for row in rows]

    async def hide_session(self, owner: uuid.UUID, session: uuid.UUID, at: datetime) -> None:
        async with self._pool.acquire() as connection, connection.transaction():
            # The row first, then the question: a second statement's snapshot sees a turn
            # that `start_turn` committed under the lock, where one UPDATE's would not.
            held = await connection.fetchval(_VISIBLE + " FOR NO KEY UPDATE", session, owner)
            if held is None:
                raise SessionNotFoundError(str(session))
            status = await connection.execute(
                "UPDATE sessions SET deleted_at = $2 WHERE id = $1 AND NOT EXISTS"
                " (SELECT 1 FROM turns WHERE session_id = $1 AND state = 'running')",
                session,
                at,
            )
            if _rows(status) == 0:
                raise TurnActiveError(str(session))

    async def purge_session(self, owner: uuid.UUID, session: uuid.UUID) -> None:
        await self._pool.execute(
            "DELETE FROM sessions WHERE id = $1 AND owner_id = $2", session, owner
        )

    async def messages_of(self, owner: uuid.UUID, session: uuid.UUID) -> list[Document]:
        async with self._pool.acquire() as connection:
            await self._visible(connection, owner, session)
            rows = await connection.fetch(
                "SELECT document FROM messages WHERE session_id = $1 ORDER BY created_at, id",
                session,
            )
        return [row["document"] for row in rows]

    # --- turns --------------------------------------------------------------

    async def start_turn(
        self, owner: uuid.UUID, turn: Turn, question: StoredMessage | None
    ) -> None:
        try:
            async with self._pool.acquire() as connection, connection.transaction():
                held = await connection.fetchval(_VISIBLE + " FOR SHARE", turn.session_id, owner)
                if held is None:
                    raise SessionNotFoundError(str(turn.session_id))
                if question is not None:
                    await self._insert_message(connection, question)
                await connection.execute(
                    f"INSERT INTO turns ({_TURN_COLUMNS})"
                    " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)",
                    turn.id,
                    turn.session_id,
                    turn.follows,
                    turn.model,
                    turn.state.value,
                    turn.started_at,
                    turn.ended_at,
                    turn.error,
                    turn.lease_until,
                    turn.retries,
                    turn.deadline_at,
                    turn.worker_id,
                    turn.attempt,
                    turn.heartbeat_at,
                    turn.cancel_requested_at,
                )
        except asyncpg.UniqueViolationError as violated:
            if violated.constraint_name == ONE_RUNNING:
                raise TurnActiveError(str(turn.session_id)) from violated
            raise

    async def append_events(
        self,
        owner: uuid.UUID,
        session: uuid.UUID,
        turn: uuid.UUID,
        fence: Fence,
        events: Sequence[StoredEvent],
        written_at: datetime,
    ) -> None:
        if not events:
            return
        try:
            async with self._pool.acquire() as connection, connection.transaction():
                # The turn row shared first: an end, which updates it, waits for the batch
                # or the batch for the end, and never one inside the other.
                held = await connection.fetchval(
                    _HELD, owner, session, turn, written_at, fence.worker, fence.attempt
                )
                if held is None:
                    raise TurnLostError(f"turn {turn} is not running under this holder")
                await self._insert_events(connection, turn, events)
                last = max(e.position for e in events)
                await connection.execute("SELECT pg_notify($1, $2)", CHANNEL, f"{turn} {last}")
        except asyncpg.UniqueViolationError as violated:
            if violated.constraint_name != POSITION_TAKEN:
                raise
            # After the rollback: the same documents are the runner's own write, acknowledged
            # late; another is a second runner.
            rows = await self._pool.fetch(
                "SELECT e.position, e.document FROM turn_events AS e"
                " JOIN turns AS t ON t.id = e.turn_id"
                " JOIN sessions AS s ON s.id = t.session_id"
                " WHERE e.turn_id = $3 AND e.position = ANY($4::integer[])"
                "   AND t.session_id = $2 AND s.owner_id = $1",
                owner,
                session,
                turn,
                [e.position for e in events],
            )
            held_already = {row["position"]: row["document"] for row in rows}
            if any(held_already.get(e.position) != e.document for e in events):
                raise TurnLostError(f"a position of turn {turn} holds another event") from violated

    async def last_position(self, owner: uuid.UUID, session: uuid.UUID, turn: uuid.UUID) -> int:
        async with self._pool.acquire() as connection:
            await self._visible(connection, owner, session)
            return await connection.fetchval(
                "SELECT coalesce(max(e.position), 0) FROM turn_events AS e"
                " JOIN turns AS t ON t.id = e.turn_id"
                " WHERE t.id = $2 AND t.session_id = $1",
                session,
                turn,
            )

    async def events_after(
        self, owner: uuid.UUID, session: uuid.UUID, turn: uuid.UUID, position: int
    ) -> list[tuple[int, Document]]:
        async with self._pool.acquire() as connection:
            await self._visible(connection, owner, session)
            rows = await connection.fetch(
                "SELECT e.position, e.document FROM turn_events AS e"
                " JOIN turns AS t ON t.id = e.turn_id"
                " WHERE t.id = $2 AND t.session_id = $1 AND e.position > $3"
                " ORDER BY e.position",
                session,
                turn,
                position,
            )
        return [(row["position"], row["document"]) for row in rows]

    async def finish_turn(
        self,
        owner: uuid.UUID,
        session: uuid.UUID,
        turn: uuid.UUID,
        fence: Fence,
        state: TurnState,
        ended_at: datetime,
        error: str | None,
        answer: StoredMessage | None,
        events: Sequence[StoredEvent],
        updated_at: datetime,
    ) -> None:
        try:
            async with self._pool.acquire() as connection, connection.transaction():
                # The session row first, then the turn: the lock order of every transaction
                # that writes both.
                status = await connection.execute(
                    "UPDATE sessions SET updated_at = $3"
                    " WHERE id = $1 AND owner_id = $2 AND deleted_at IS NULL",
                    session,
                    owner,
                    updated_at,
                )
                if _rows(status) == 0:
                    raise SessionNotFoundError(str(session))
                status = await connection.execute(
                    "UPDATE turns SET state = $3, ended_at = $4, error = $5"
                    " WHERE id = $1 AND session_id = $2 AND state = 'running'"
                    "   AND lease_until > $4 AND worker_id = $6 AND attempt = $7",
                    turn,
                    session,
                    state.value,
                    ended_at,
                    error,
                    fence.worker,
                    fence.attempt,
                )
                if _rows(status) == 0:
                    raise TurnLostError(f"turn {turn} is not running")
                if answer is not None:
                    await self._insert_message(connection, answer)
                await self._insert_events(connection, turn, events)
                await connection.execute("SELECT pg_notify($1, $2)", CHANNEL, f"{turn} end")
        except asyncpg.UniqueViolationError as violated:
            if violated.constraint_name in (POSITION_TAKEN, "messages_pkey"):
                raise TurnLostError(f"turn {turn} was finished already") from violated
            raise

    async def end_expired_turn(
        self,
        owner: uuid.UUID,
        session: uuid.UUID,
        turn: uuid.UUID,
        state: TurnState,
        now: datetime,
        error: str | None,
        answer: StoredMessage | None,
        events: Sequence[StoredEvent],
    ) -> Turn | None:
        try:
            async with self._pool.acquire() as connection, connection.transaction():
                # The session row first, then the turn: the lock order of every transaction
                # that writes both.
                held = await connection.fetchval(_VISIBLE + " FOR NO KEY UPDATE", session, owner)
                if held is None:
                    raise SessionNotFoundError(str(session))
                row = await connection.fetchrow(
                    _END_EXPIRED, turn, session, state.value, now, error
                )
                if row is None:
                    return None
                await connection.execute(
                    "UPDATE sessions SET updated_at = $2 WHERE id = $1", session, now
                )
                if answer is not None:
                    await self._insert_message(connection, answer)
                await self._insert_events(connection, turn, events)
                await connection.execute("SELECT pg_notify($1, $2)", CHANNEL, f"{turn} end")
        except asyncpg.UniqueViolationError as violated:
            if violated.constraint_name in (POSITION_TAKEN, "messages_pkey"):
                raise TurnLostError(f"turn {turn} wrote an event meanwhile") from violated
            raise
        return _turn(row)

    async def request_cancel(
        self, owner: uuid.UUID, session: uuid.UUID, turn: uuid.UUID, now: datetime
    ) -> Turn | None:
        async with self._pool.acquire() as connection, connection.transaction():
            await self._visible(connection, owner, session)
            row = await connection.fetchrow(
                "UPDATE turns SET cancel_requested_at = coalesce(cancel_requested_at, $3)"
                " WHERE id = $1 AND session_id = $2 AND state = 'running'"
                f" RETURNING {_TURN_COLUMNS}",
                turn,
                session,
                now,
            )
            if row is not None:
                await connection.execute("SELECT pg_notify($1, $2)", CANCEL_CHANNEL, str(turn))
                return _turn(row)
            row = await connection.fetchrow(
                f"SELECT {_TURN_COLUMNS} FROM turns WHERE id = $1 AND session_id = $2",
                turn,
                session,
            )
        return None if row is None else _turn(row)

    async def active_turn(self, owner: uuid.UUID, session: uuid.UUID) -> Turn | None:
        async with self._pool.acquire() as connection:
            await self._visible(connection, owner, session)
            row = await connection.fetchrow(
                f"SELECT {_TURN_COLUMNS} FROM turns WHERE session_id = $1 AND state = 'running'",
                session,
            )
        return None if row is None else _turn(row)

    async def latest_turn(self, owner: uuid.UUID, session: uuid.UUID) -> Turn | None:
        async with self._pool.acquire() as connection:
            await self._visible(connection, owner, session)
            row = await connection.fetchrow(
                f"SELECT {_TURN_COLUMNS} FROM turns WHERE session_id = $1"
                " ORDER BY started_at DESC, id DESC LIMIT 1",
                session,
            )
        return None if row is None else _turn(row)

    async def get_turn(self, owner: uuid.UUID, session: uuid.UUID, turn: uuid.UUID) -> Turn | None:
        async with self._pool.acquire() as connection:
            await self._visible(connection, owner, session)
            row = await connection.fetchrow(
                f"SELECT {_TURN_COLUMNS} FROM turns WHERE id = $1 AND session_id = $2",
                turn,
                session,
            )
        return None if row is None else _turn(row)

    async def wait_for_events(
        self, owner: uuid.UUID, session: uuid.UUID, turn: uuid.UUID, after: int, timeout: float
    ) -> bool:
        await self._listen()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            woken: asyncio.Future[None] = loop.create_future()
            self._waiters.setdefault(turn, set()).add(woken)
            try:
                if await self._ready(owner, session, turn, after):
                    return True
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return False
                try:
                    await asyncio.wait_for(asyncio.shield(woken), remaining)
                except TimeoutError:
                    return await self._ready(owner, session, turn, after)
            finally:
                waiting = self._waiters.get(turn)
                if waiting is not None:
                    waiting.discard(woken)
                    if not waiting:
                        del self._waiters[turn]

    # --- work ---------------------------------------------------------------

    async def heartbeat(
        self, worker: str, held: Sequence[Held], now: datetime, lease: timedelta
    ) -> Renewed:
        if not held:
            return Renewed({})
        rows = await self._on_work_connection(
            lambda connection: connection.fetch(
                _HEARTBEAT,
                worker,
                now,
                now + lease,
                [h.turn for h in held],
                [h.attempt for h in held],
            )
        )
        return Renewed({row["id"]: row["cancel_requested_at"] for row in rows})

    async def cancel_signals(self) -> AsyncIterator[uuid.UUID]:
        signals: asyncio.Queue[uuid.UUID] = asyncio.Queue()
        self._cancels.add(signals)
        try:
            while True:
                try:
                    await self._listen()
                except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError):
                    # The database is away: the heartbeat reads the asks back once it is not.
                    log.warning("the listening connection could not be opened; trying again")
                try:
                    yield await asyncio.wait_for(signals.get(), LISTENER_CHECK)
                except TimeoutError:
                    continue
        finally:
            self._cancels.discard(signals)

    async def _on_work_connection(
        self, statement: Callable[[asyncpg.Connection], Awaitable[T]]
    ) -> T:
        """One statement on the work connection: this process's own, outside the pool, so
        that a pool every request is waiting on never delays a lease. Opened on first use,
        one statement at a time, and opened again after it breaks."""
        if self._work_lock is None:
            self._work_lock = asyncio.Lock()
        async with self._work_lock:
            if self._work is None or self._work.is_closed():
                if self._dsn is None:
                    raise RuntimeError("a store that renews leases needs the database's dsn")
                self._work = await asyncpg.connect(self._dsn)
            try:
                return await statement(self._work)
            except (asyncpg.InterfaceError, OSError):
                broken, self._work = self._work, None
                broken.terminate()
                raise

    # --- helpers ------------------------------------------------------------

    async def _ready(
        self, owner: uuid.UUID, session: uuid.UUID, turn: uuid.UUID, after: int
    ) -> bool:
        row = await self._pool.fetchrow(
            "SELECT t.state, EXISTS (SELECT 1 FROM turn_events AS e"
            "   WHERE e.turn_id = t.id AND e.position > $4) AS more"
            " FROM turns AS t JOIN sessions AS s ON s.id = t.session_id"
            " WHERE t.id = $3 AND t.session_id = $2 AND s.owner_id = $1",
            owner,
            session,
            turn,
            after,
        )
        return row is None or row["more"] or row["state"] != TurnState.RUNNING.value

    async def _listen(self) -> None:
        """The one listening connection, opened on first use, apart from the pool."""
        if self._listener is not None and not self._listener.is_closed():
            return
        if self._opening is None:
            self._opening = asyncio.Lock()
        async with self._opening:
            if self._listener is not None and not self._listener.is_closed():
                return
            if self._dsn is None:
                raise RuntimeError("a store that waits for events needs the database's dsn")
            listener = await asyncpg.connect(self._dsn)
            await listener.add_listener(CHANNEL, self._notified)
            await listener.add_listener(CANCEL_CHANNEL, self._cancel_notified)
            self._listener = listener

    def _notified(self, connection: object, pid: int, channel: str, payload: str) -> None:
        head, _, _ = payload.partition(" ")
        try:
            turn = uuid.UUID(head)
        except ValueError:
            return
        for woken in self._waiters.get(turn, ()):
            if not woken.done():
                woken.set_result(None)

    def _cancel_notified(self, connection: object, pid: int, channel: str, payload: str) -> None:
        try:
            turn = uuid.UUID(payload)
        except ValueError:
            return
        for signals in self._cancels:
            signals.put_nowait(turn)

    async def _visible(
        self, connection: asyncpg.Connection, owner: uuid.UUID, session: uuid.UUID
    ) -> None:
        if await connection.fetchval(_VISIBLE, session, owner) is None:
            raise SessionNotFoundError(str(session))

    async def _insert_events(
        self, connection: asyncpg.Connection, turn: uuid.UUID, events: Sequence[StoredEvent]
    ) -> None:
        if events:
            await connection.executemany(
                "INSERT INTO turn_events (turn_id, position, document, expires_at)"
                " VALUES ($1, $2, $3, $4)",
                [(turn, e.position, e.document, e.expires_at) for e in events],
            )

    async def _insert_message(self, connection: asyncpg.Connection, message: StoredMessage) -> None:
        await connection.execute(
            "INSERT INTO messages (id, session_id, parent_id, role, created_at, document)"
            " VALUES ($1, $2, $3, $4, $5, $6)",
            message.id,
            message.session_id,
            message.parent_id,
            message.role.value if isinstance(message.role, Role) else message.role,
            message.created_at,
            message.document,
        )
