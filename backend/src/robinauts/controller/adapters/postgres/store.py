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
costs a timeout and nothing else. The heartbeat runs on a third connection, the work
connection, also apart from the pool, so that a pool busy with requests never delays a lease.
Every write of a runner names its holder, the pod and the attempt, beside the lease.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta
from typing import Any

import asyncpg

from robinauts.controller.adapters.postgres.pool import codecs, open_pool
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
    Holder,
    Store,
    StoredEvent,
    StoredMessage,
)
from robinauts.controller.ports.work import HeartbeatResult, Held, WorkQueue

CHANNEL = "robinauts_turns"
"""Where a turn's writes are announced: ``<turn> <position>``, or ``<turn> end``."""

ONE_RUNNING = "turns_one_running_per_session"
POSITION_TAKEN = "turn_events_pkey"
USER_EXISTS = "users_provider_subject_key"

_SESSION_COLUMNS = "id, owner_id, agent, engine, title, created_at, updated_at"
_TURN_COLUMNS = (
    "id, session_id, follows, model, state, started_at, ended_at, error, lease_until, retries,"
    " deadline_at, worker_id, attempt, heartbeat_at, cancel_requested_at"
)


def _held_by(worker: str, attempt: str) -> str:
    """A write's fence, over the parameters named: the turn is still that holder's, when one
    is named."""
    return f"({worker}::text IS NULL OR (t.worker_id = {worker} AND t.attempt = {attempt}))"


_VISIBLE = "SELECT 1 FROM sessions WHERE id = $1 AND owner_id = $2 AND deleted_at IS NULL"

_APPEND = f"""
INSERT INTO turn_events (turn_id, position, document, expires_at)
SELECT t.id, $4, $5, $6
FROM turns AS t
JOIN sessions AS s ON s.id = t.session_id
WHERE t.id = $3 AND t.session_id = $2 AND s.owner_id = $1 AND s.deleted_at IS NULL
  AND t.state = 'running' AND t.lease_until > $7
  AND {_held_by("$8", "$9")}
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
UPDATE turns AS t
SET state = 'interrupted', ended_at = $3, error = 'lease expired'
FROM sessions AS s
WHERE s.id = t.session_id AND s.id = $2 AND s.owner_id = $1 AND s.deleted_at IS NULL
  AND t.state = 'running' AND t.lease_until < $3 AND ($4::uuid IS NULL OR t.id = $4)
RETURNING {", ".join("t." + c for c in _TURN_COLUMNS.split(", "))}
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
    makes its own pool, checks the schema, and ``close`` closes it. The listener and the work
    connection are opened from the dsn, on first use, and closed by ``close``."""

    def __init__(self, pool: asyncpg.Pool | None = None, dsn: str | None = None) -> None:
        self._pool: asyncpg.Pool = pool  # type: ignore[assignment]
        self._owns_pool = pool is None
        self._dsn = dsn
        self._listener: asyncpg.Connection | None = None
        self._waiters: dict[uuid.UUID, set[asyncio.Future[None]]] = {}
        self._opening: asyncio.Lock | None = None
        self._work: asyncpg.Connection | None = None
        self._working: asyncio.Lock | None = None
        self._search_path: str | None = None

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
        # The connections apart from the pool see the tables the pool sees.
        self._search_path = await self._pool.fetchval("SELECT current_setting('search_path')")
        return self._pool

    async def close(self) -> None:
        if self._listener is not None:
            listener, self._listener = self._listener, None
            await listener.close()
        if self._work is not None:
            work, self._work = self._work, None
            await work.close()
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

    async def append_event(
        self,
        owner: uuid.UUID,
        session: uuid.UUID,
        turn: uuid.UUID,
        position: int,
        document: Document,
        written_at: datetime,
        expires_at: datetime,
        *,
        holder: Holder | None = None,
    ) -> None:
        worker, attempt = (None, None) if holder is None else (holder.worker_id, holder.attempt)
        try:
            async with self._pool.acquire() as connection, connection.transaction():
                status = await connection.execute(
                    _APPEND,
                    owner,
                    session,
                    turn,
                    position,
                    document,
                    expires_at,
                    written_at,
                    worker,
                    attempt,
                )
                if _rows(status) == 0:
                    raise TurnLostError(f"turn {turn} is not running")
                await connection.execute("SELECT pg_notify($1, $2)", CHANNEL, f"{turn} {position}")
        except asyncpg.UniqueViolationError as violated:
            if violated.constraint_name != POSITION_TAKEN:
                raise
            # After the rollback: the same document is the runner's own write, acknowledged
            # late; another is a second runner.
            held = await self._pool.fetchval(
                "SELECT e.document FROM turn_events AS e"
                " JOIN turns AS t ON t.id = e.turn_id"
                " JOIN sessions AS s ON s.id = t.session_id"
                " WHERE e.turn_id = $3 AND e.position = $4 AND t.session_id = $2"
                "   AND s.owner_id = $1",
                owner,
                session,
                turn,
                position,
            )
            if held != document:
                raise TurnLostError(
                    f"position {position} of turn {turn} holds another event"
                ) from violated

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
        state: TurnState,
        ended_at: datetime,
        error: str | None,
        answer: StoredMessage | None,
        events: Sequence[StoredEvent],
        updated_at: datetime,
        *,
        holder: Holder | None = None,
    ) -> None:
        worker, attempt = (None, None) if holder is None else (holder.worker_id, holder.attempt)
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
                    "UPDATE turns AS t SET state = $3, ended_at = $4, error = $5"
                    " WHERE t.id = $1 AND t.session_id = $2 AND t.state = 'running'"
                    f"   AND t.lease_until > $4 AND {_held_by('$6', '$7')}",
                    turn,
                    session,
                    state.value,
                    ended_at,
                    error,
                    worker,
                    attempt,
                )
                if _rows(status) == 0:
                    raise TurnLostError(f"turn {turn} is not running")
                if answer is not None:
                    await self._insert_message(connection, answer)
                for event in events:
                    await connection.execute(
                        "INSERT INTO turn_events (turn_id, position, document, expires_at)"
                        " VALUES ($1, $2, $3, $4)",
                        turn,
                        event.position,
                        event.document,
                        event.expires_at,
                    )
                await connection.execute("SELECT pg_notify($1, $2)", CHANNEL, f"{turn} end")
        except asyncpg.UniqueViolationError as violated:
            if violated.constraint_name in (POSITION_TAKEN, "messages_pkey"):
                raise TurnLostError(f"turn {turn} was finished already") from violated
            raise

    async def end_expired_turn(
        self,
        owner: uuid.UUID,
        session: uuid.UUID,
        now: datetime,
        *,
        turn: uuid.UUID | None = None,
        answer: StoredMessage | None = None,
    ) -> Turn | None:
        async with self._pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(_END_EXPIRED, owner, session, now, turn)
            if row is None:
                return None
            if answer is not None:
                # Only the reader whose write ended the turn gets here, so the answer is
                # stored once.
                await self._insert_message(connection, answer)
            await connection.execute("SELECT pg_notify($1, $2)", CHANNEL, f"{row['id']} end")
        return _turn(row)

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
    ) -> HeartbeatResult:
        if not held:
            return HeartbeatResult()
        rows = await self._working_on(
            lambda connection: connection.fetch(
                _HEARTBEAT,
                worker,
                now,
                now + lease,
                [h.turn_id for h in held],
                [h.attempt for h in held],
            )
        )
        renewed = {row["id"] for row in rows}
        return HeartbeatResult(
            lost=frozenset(h.turn_id for h in held) - renewed,
            cancelled=frozenset(r["id"] for r in rows if r["cancel_requested_at"] is not None),
        )

    async def _working_on(self, statement: Callable[[asyncpg.Connection], Awaitable[Any]]) -> Any:
        """One statement on the work connection, opened on first use and again after a
        failure, with the ``search_path`` the pool had at ``open``; one statement at a
        time."""
        if self._working is None:
            self._working = asyncio.Lock()
        async with self._working:
            if self._work is None or self._work.is_closed():
                if self._dsn is None:
                    raise RuntimeError("a store that keeps leases needs the database's dsn")
                if self._search_path is None:
                    raise RuntimeError("the store is not open")
                self._work = await asyncpg.connect(
                    self._dsn, server_settings={"search_path": self._search_path}
                )
                await codecs(self._work)
            try:
                return await statement(self._work)
            except (asyncpg.PostgresConnectionStatusError, asyncpg.InterfaceError, OSError):
                work, self._work = self._work, None
                work.terminate()
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

    async def _visible(
        self, connection: asyncpg.Connection, owner: uuid.UUID, session: uuid.UUID
    ) -> None:
        if await connection.fetchval(_VISIBLE, session, owner) is None:
            raise SessionNotFoundError(str(session))

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
