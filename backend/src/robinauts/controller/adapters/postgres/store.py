# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The store over asyncpg, keeping ``schema.sql``'s rendering of the data model.

Every operation checks the owner and ``deleted_at`` itself, as the port promises. The
session row is locked first in every transaction that writes a session and one of its
turns: ``start_turn`` with ``FOR SHARE``, ``finish_turn`` with an ``UPDATE``, so that the
two never deadlock. Constraint names are the interface with the database: a violation is
translated by name. Watchers are woken by ``NOTIFY`` on one listening connection per store,
held apart from the pool, and read the store again in every case, so a notification lost
with a dropped connection costs a timeout and nothing else. A request to stop a turn comes
on the same connection, and one that is lost is read back with the next renewal of the
turn's lease. So does a queued turn, which a worker that misses it finds at its next wait.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime

import asyncpg

from robinauts.controller.adapters.postgres.pool import COMMAND_TIMEOUT, open_pool
from robinauts.controller.adapters.postgres.schema import check_schema
from robinauts.controller.contract.domain import (
    ACTIVE,
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

_log = logging.getLogger(__name__)

CHANNEL = "robinauts_turns"
"""Where a turn's writes are announced: ``<turn> <position>``, or ``<turn> end``."""

CANCEL_CHANNEL = "robinauts_cancel"
"""Where a request to stop a turn is announced: ``<turn>``."""

QUEUED_CHANNEL = "robinauts_queued"
"""Where a queued turn is announced: ``<turn>``."""

ONE_ACTIVE = "turns_one_active_per_session"
POSITION_TAKEN = "turn_events_pkey"
USER_EXISTS = "users_provider_subject_key"

_SESSION_COLUMNS = "id, owner_id, agent, engine, title, created_at, updated_at"
_TURN_COLUMNS = (
    "id, session_id, follows, model, state, started_at, ended_at, error, lease_until, retries"
)

_VISIBLE = "SELECT 1 FROM sessions WHERE id = $1 AND owner_id = $2 AND deleted_at IS NULL"

_APPEND = """
INSERT INTO turn_events (turn_id, position, document, expires_at)
SELECT t.id, $4, $5, $6
FROM turns AS t
JOIN sessions AS s ON s.id = t.session_id
WHERE t.id = $3 AND t.session_id = $2 AND s.owner_id = $1 AND s.deleted_at IS NULL
  AND t.state = 'running' AND t.lease_until > $7
FOR SHARE OF t
"""

_NOT_ACTIVE = """
  AND NOT EXISTS (SELECT 1 FROM turns AS t WHERE t.session_id = s.id
    AND t.state IN ('queued', 'running') AND t.lease_until > $1)
"""

_T_COLUMNS = ", ".join("t." + c for c in _TURN_COLUMNS.split(", "))

_END_EXPIRED = f"""
UPDATE turns AS t
SET state = 'interrupted', ended_at = $3, error = 'lease expired'
FROM sessions AS s
WHERE s.id = t.session_id AND s.id = $2 AND s.owner_id = $1 AND s.deleted_at IS NULL
  AND t.id = $4 AND t.state IN ('queued', 'running') AND t.lease_until < $3
RETURNING {_T_COLUMNS}
"""

_CLAIM = f"""
UPDATE turns AS t
SET state = 'running', lease_until = $2
FROM sessions AS s
WHERE s.id = t.session_id AND t.state = 'queued' AND t.id IN (
  SELECT q.id FROM turns AS q
  JOIN sessions AS qs ON qs.id = q.session_id
  WHERE q.state = 'queued' AND q.lease_until > $1 AND qs.deleted_at IS NULL
  ORDER BY q.started_at, q.id
  LIMIT $3
  FOR UPDATE OF q SKIP LOCKED
)
RETURNING s.owner_id, {_T_COLUMNS}
"""

_CLAIMABLE = """
SELECT EXISTS (SELECT 1 FROM turns AS t JOIN sessions AS s ON s.id = t.session_id
  WHERE t.state = 'queued' AND t.lease_until > $1 AND s.deleted_at IS NULL)
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
    )


def _user(row: asyncpg.Record) -> User:
    return User(
        row["id"], row["provider"], row["subject"], row["name"], row["email"], row["created_at"]
    )


def _rows(status: str) -> int:
    """The row count at the end of a command tag such as ``UPDATE 1``."""
    return int(status.rsplit(" ", 1)[-1])


class PostgresStore(Store):
    """Over a pool it is given, which the giver closes, or over a dsn, from which ``open``
    makes its own pool, checks the schema, and ``close`` closes it."""

    def __init__(self, pool: asyncpg.Pool | None = None, dsn: str | None = None) -> None:
        self._pool: asyncpg.Pool = pool  # type: ignore[assignment]
        self._owns_pool = pool is None
        self._dsn = dsn
        self._listener: asyncpg.Connection | None = None
        self._waiters: dict[uuid.UUID, set[asyncio.Future[None]]] = {}
        self._queued_waiters: set[asyncio.Future[None]] = set()
        self._stops: list[Callable[[uuid.UUID], object]] = []
        self._opening: asyncio.Lock | None = None

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
        if self._listener is not None:
            listener, self._listener = self._listener, None
            await listener.close()
        if self._owns_pool and self._pool is not None:
            pool, self._pool = self._pool, None  # type: ignore[assignment]
            # Closing waits for every connection to come back: a task holding one hangs it.
            try:
                await asyncio.wait_for(pool.close(), COMMAND_TIMEOUT)
            except TimeoutError:
                _log.warning(
                    "the pool did not close in %s s, as a query or a task still held a"
                    " connection: terminated",
                    COMMAND_TIMEOUT,
                )
                pool.terminate()

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
        status = await self._pool.execute(
            "UPDATE sessions SET deleted_at = $3"
            " WHERE id = $1 AND owner_id = $2 AND deleted_at IS NULL",
            session,
            owner,
            at,
        )
        if _rows(status) == 0:
            raise SessionNotFoundError(str(session))

    async def purge_session(self, owner: uuid.UUID, session: uuid.UUID, now: datetime) -> bool:
        status = await self._pool.execute(
            "DELETE FROM sessions AS s WHERE id = $2 AND owner_id = $3" + _NOT_ACTIVE,
            now,
            session,
            owner,
        )
        return _rows(status) > 0

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
                    " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
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
                )
                if turn.state is TurnState.QUEUED:
                    await connection.execute(
                        "SELECT pg_notify($1, $2)", QUEUED_CHANNEL, str(turn.id)
                    )
        except asyncpg.UniqueViolationError as violated:
            if violated.constraint_name == ONE_ACTIVE:
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
    ) -> None:
        try:
            async with self._pool.acquire() as connection, connection.transaction():
                status = await connection.execute(
                    _APPEND, owner, session, turn, position, document, expires_at, written_at
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
                    "   AND lease_until > $4",
                    turn,
                    session,
                    state.value,
                    ended_at,
                    error,
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
        turn: uuid.UUID,
        now: datetime,
        answer: StoredMessage | None = None,
    ) -> Turn | None:
        async with self._pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(_END_EXPIRED, owner, session, now, turn)
            if row is None:
                return None
            if answer is not None:
                await self._insert_message(connection, answer)
            await connection.execute("SELECT pg_notify($1, $2)", CHANNEL, f"{row['id']} end")
        return _turn(row)

    async def renew_leases(
        self, turns: Sequence[uuid.UUID], now: datetime, until: datetime
    ) -> list[uuid.UUID]:
        rows = await self._pool.fetch(
            "UPDATE turns SET lease_until = $3"
            " WHERE id = ANY($1) AND state = 'running' AND lease_until > $2"
            " RETURNING id, cancel_requested_at",
            list(turns),
            now,
            until,
        )
        return [row["id"] for row in rows if row["cancel_requested_at"] is not None]

    async def claim_turns(
        self, now: datetime, until: datetime, limit: int
    ) -> list[tuple[uuid.UUID, Turn]]:
        rows = await self._pool.fetch(_CLAIM, now, until, limit)
        return [(row["owner_id"], _turn(row)) for row in rows]

    async def wait_for_queued(self, now: datetime, timeout: float) -> bool:
        await self._listen()
        woken: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._queued_waiters.add(woken)
        try:
            if await self._pool.fetchval(_CLAIMABLE, now):
                return True
            try:
                await asyncio.wait_for(asyncio.shield(woken), timeout)
            except TimeoutError:
                return False
            return True
        finally:
            self._queued_waiters.discard(woken)

    async def request_cancel(
        self, owner: uuid.UUID, session: uuid.UUID, turn: uuid.UUID, at: datetime
    ) -> None:
        async with self._pool.acquire() as connection, connection.transaction():
            # The state on the right of each SET is the one before the update.
            state = await connection.fetchval(
                "UPDATE turns AS t SET cancel_requested_at = $4,"
                "   state = CASE WHEN t.state = 'queued' THEN 'cancelled' ELSE t.state END,"
                "   ended_at = CASE WHEN t.state = 'queued' THEN $4 END"
                " FROM sessions AS s"
                " WHERE s.id = t.session_id AND s.id = $2 AND s.owner_id = $1"
                "   AND s.deleted_at IS NULL AND t.id = $3 AND t.state IN ('queued', 'running')"
                " RETURNING t.state",
                owner,
                session,
                turn,
                at,
            )
            if state == TurnState.CANCELLED.value:
                await connection.execute("SELECT pg_notify($1, $2)", CHANNEL, f"{turn} end")
            elif state == TurnState.RUNNING.value:
                await connection.execute("SELECT pg_notify($1, $2)", CANCEL_CHANNEL, str(turn))

    async def listen_for_cancels(self, stop: Callable[[uuid.UUID], object]) -> None:
        self._stops.append(stop)
        await self._listen()

    async def hidden_sessions(self, now: datetime) -> list[Session]:
        rows = await self._pool.fetch(
            f"SELECT {_SESSION_COLUMNS} FROM sessions AS s"
            " WHERE deleted_at IS NOT NULL" + _NOT_ACTIVE,
            now,
        )
        return [_session(row) for row in rows]

    async def expired_turns(self, now: datetime) -> list[tuple[uuid.UUID, Turn]]:
        rows = await self._pool.fetch(
            f"SELECT s.owner_id, {_T_COLUMNS} FROM turns AS t"
            " JOIN sessions AS s ON s.id = t.session_id"
            " WHERE t.state IN ('queued', 'running') AND t.lease_until < $1"
            "   AND s.deleted_at IS NULL",
            now,
        )
        return [(row["owner_id"], _turn(row)) for row in rows]

    async def active_turn(self, owner: uuid.UUID, session: uuid.UUID) -> Turn | None:
        async with self._pool.acquire() as connection:
            await self._visible(connection, owner, session)
            row = await connection.fetchrow(
                f"SELECT {_TURN_COLUMNS} FROM turns"
                " WHERE session_id = $1 AND state IN ('queued', 'running')",
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
        return row is None or row["more"] or TurnState(row["state"]) not in ACTIVE

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
            await listener.add_listener(CANCEL_CHANNEL, self._cancelled)
            await listener.add_listener(QUEUED_CHANNEL, self._queued)
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

    def _queued(self, connection: object, pid: int, channel: str, payload: str) -> None:
        for woken in self._queued_waiters:
            if not woken.done():
                woken.set_result(None)

    def _cancelled(self, connection: object, pid: int, channel: str, payload: str) -> None:
        try:
            turn = uuid.UUID(payload)
        except ValueError:
            return
        for stop in self._stops:
            stop(turn)

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
