# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The store over dicts, in this process, keeping the model of ``data-model.md`` by the same
keys a partitioned store would."""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timedelta

from robinauts.controller.contract.domain import (
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


def _held_by(turn: Turn, holder: Holder | None) -> bool:
    return holder is None or (turn.worker_id, turn.attempt) == (holder.worker_id, holder.attempt)


class MemoryStore(Store, WorkQueue):
    def __init__(self) -> None:
        self._users: dict[tuple[str, str], User] = {}
        self._sessions: dict[uuid.UUID, Session] = {}
        self._hidden: dict[uuid.UUID, datetime] = {}
        self._messages: dict[uuid.UUID, list[StoredMessage]] = {}
        self._turns: dict[uuid.UUID, Turn] = {}
        self._events: dict[uuid.UUID, list[StoredEvent]] = {}
        self._changed = asyncio.Condition()
        self._cancels: set[asyncio.Queue[uuid.UUID]] = set()

    async def open(self) -> None:
        """Nothing to open: the engines keep their memory in this process too."""

    async def close(self) -> None:
        """Nothing to release."""

    # --- helpers ------------------------------------------------------------

    def _visible(self, owner: uuid.UUID, session: uuid.UUID) -> Session:
        found = self._sessions.get(session)
        if found is None or found.owner_id != owner or session in self._hidden:
            raise SessionNotFoundError(str(session))
        return found

    def _running(self, session: uuid.UUID) -> Turn | None:
        return next(
            (
                t
                for t in self._turns.values()
                if t.session_id == session and t.state is TurnState.RUNNING
            ),
            None,
        )

    def _turn_of(self, session: uuid.UUID, turn: uuid.UUID) -> Turn | None:
        found = self._turns.get(turn)
        return found if found is not None and found.session_id == session else None

    async def _notify(self) -> None:
        async with self._changed:
            self._changed.notify_all()

    # --- users --------------------------------------------------------------

    async def add_user_if_absent(self, user: User) -> User:
        return self._users.setdefault((user.provider, user.subject), user)

    def user_by_id(self, user_id: uuid.UUID) -> User | None:
        """Not the port's: what ``MemoryCredentials`` resolves a secret to."""
        return next((u for u in self._users.values() if u.id == user_id), None)

    # --- sessions -----------------------------------------------------------

    async def add_session(self, session: Session) -> None:
        self._sessions[session.id] = session
        self._messages[session.id] = []

    async def get_session(self, owner: uuid.UUID, session: uuid.UUID) -> Session:
        return self._visible(owner, session)

    async def update_session(self, session: Session) -> None:
        self._sessions[session.id] = session

    async def sessions_of(
        self, owner: uuid.UUID, limit: int, before: Cursor | None
    ) -> list[Session]:
        owned = [
            s
            for s in self._sessions.values()
            if s.owner_id == owner
            and s.id not in self._hidden
            and (before is None or (s.updated_at, s.id) < before)
        ]
        owned.sort(key=lambda s: (s.updated_at, s.id), reverse=True)
        return owned[:limit]

    async def hide_session(self, owner: uuid.UUID, session: uuid.UUID, at: datetime) -> None:
        self._visible(owner, session)
        if self._running(session) is not None:
            raise TurnActiveError(str(session))
        self._hidden[session] = at
        await self._notify()

    async def purge_session(self, owner: uuid.UUID, session: uuid.UUID) -> None:
        found = self._sessions.get(session)
        if found is None or found.owner_id != owner:
            return
        del self._sessions[session]
        self._hidden.pop(session, None)
        self._messages.pop(session, None)
        for turn in [t for t in self._turns.values() if t.session_id == session]:
            del self._turns[turn.id]
            self._events.pop(turn.id, None)
        await self._notify()

    async def messages_of(self, owner: uuid.UUID, session: uuid.UUID) -> list[Document]:
        self._visible(owner, session)
        stored = sorted(self._messages[session], key=lambda m: (m.created_at, m.id))
        return [m.document for m in stored]

    # --- turns --------------------------------------------------------------

    async def start_turn(
        self, owner: uuid.UUID, turn: Turn, question: StoredMessage | None
    ) -> None:
        self._visible(owner, turn.session_id)
        if self._running(turn.session_id) is not None:
            raise TurnActiveError(str(turn.session_id))
        if question is not None:
            self._messages[turn.session_id].append(question)
        self._turns[turn.id] = turn
        self._events[turn.id] = []
        await self._notify()

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
        self._visible(owner, session)
        found = self._turn_of(session, turn)
        if (
            found is None
            or found.state is not TurnState.RUNNING
            or found.lease_until <= written_at
            or not _held_by(found, holder)
        ):
            raise TurnLostError(f"turn {turn} is not running")
        events = self._events[turn]
        taken = next((e for e in events if e.position == position), None)
        if taken is not None:
            if taken.document == document:
                return
            raise TurnLostError(f"position {position} of turn {turn} holds another event")
        events.append(StoredEvent(position, document, expires_at))
        await self._notify()

    async def events_after(
        self, owner: uuid.UUID, session: uuid.UUID, turn: uuid.UUID, position: int
    ) -> list[tuple[int, Document]]:
        self._visible(owner, session)
        if self._turn_of(session, turn) is None:
            return []
        events = sorted(self._events[turn], key=lambda e: e.position)
        return [(e.position, e.document) for e in events if e.position > position]

    async def finish_turn(
        self,
        owner: uuid.UUID,
        session: uuid.UUID,
        turn: uuid.UUID,
        state: TurnState,
        ended_at: datetime,
        error: str | None,
        answer: StoredMessage | None,
        events: list[StoredEvent] | tuple[StoredEvent, ...],
        updated_at: datetime,
        *,
        holder: Holder | None = None,
    ) -> None:
        self._visible(owner, session)
        found = self._turn_of(session, turn)
        if (
            found is None
            or found.state is not TurnState.RUNNING
            or found.lease_until <= ended_at
            or not _held_by(found, holder)
        ):
            raise TurnLostError(f"turn {turn} is not running")
        if answer is not None:
            self._messages[session].append(answer)
        self._events[turn].extend(events)
        self._turns[turn] = dataclasses.replace(found, state=state, ended_at=ended_at, error=error)
        self._sessions[session] = dataclasses.replace(
            self._sessions[session], updated_at=updated_at
        )
        await self._notify()

    async def end_expired_turn(
        self,
        owner: uuid.UUID,
        session: uuid.UUID,
        now: datetime,
        *,
        turn: uuid.UUID | None = None,
        answer: StoredMessage | None = None,
    ) -> Turn | None:
        self._visible(owner, session)
        running = self._running(session)
        if running is None or running.lease_until >= now or turn not in (None, running.id):
            return None
        if running.cancel_requested_at is not None:
            ended = dataclasses.replace(running, state=TurnState.CANCELLED, ended_at=now)
            self._turns[running.id] = ended
            await self._notify()
            return ended
        if answer is not None:
            self._messages[session].append(answer)
        ended = dataclasses.replace(
            running, state=TurnState.INTERRUPTED, ended_at=now, error="lease expired"
        )
        self._turns[running.id] = ended
        await self._notify()
        return ended

    async def request_cancel(
        self, owner: uuid.UUID, session: uuid.UUID, turn: uuid.UUID, now: datetime
    ) -> Turn | None:
        self._visible(owner, session)
        found = self._turn_of(session, turn)
        if found is None or found.state is not TurnState.RUNNING:
            return None
        if found.cancel_requested_at is None:
            found = dataclasses.replace(found, cancel_requested_at=now)
            self._turns[turn] = found
        for queue in self._cancels:
            queue.put_nowait(turn)
        return found

    async def active_turn(self, owner: uuid.UUID, session: uuid.UUID) -> Turn | None:
        self._visible(owner, session)
        return self._running(session)

    async def latest_turn(self, owner: uuid.UUID, session: uuid.UUID) -> Turn | None:
        self._visible(owner, session)
        turns = [t for t in self._turns.values() if t.session_id == session]
        return max(turns, key=lambda t: (t.started_at, t.id), default=None)

    async def get_turn(self, owner: uuid.UUID, session: uuid.UUID, turn: uuid.UUID) -> Turn | None:
        self._visible(owner, session)
        return self._turn_of(session, turn)

    # --- work ---------------------------------------------------------------

    async def heartbeat(
        self, worker: str, held: Sequence[Held], now: datetime, lease: timedelta
    ) -> HeartbeatResult:
        lost, cancelled = set(), set()
        for each in held:
            found = self._turns.get(each.turn_id)
            if (
                found is None
                or found.state is not TurnState.RUNNING
                or (found.worker_id, found.attempt) != (worker, each.attempt)
                or found.lease_until <= now
            ):
                lost.add(each.turn_id)
                continue
            self._turns[found.id] = dataclasses.replace(
                found, lease_until=now + lease, heartbeat_at=now
            )
            if found.cancel_requested_at is not None:
                cancelled.add(found.id)
        return HeartbeatResult(frozenset(lost), frozenset(cancelled))

    async def end_cancelled(self, worker: str, held: Held, now: datetime) -> bool:
        found = self._turns.get(held.turn_id)
        if (
            found is None
            or found.state is not TurnState.RUNNING
            or (found.worker_id, found.attempt) != (worker, held.attempt)
        ):
            return False
        self._turns[found.id] = dataclasses.replace(found, state=TurnState.CANCELLED, ended_at=now)
        await self._notify()
        return True

    def cancel_signals(self) -> AsyncIterator[uuid.UUID]:
        queue: asyncio.Queue[uuid.UUID] = asyncio.Queue()
        self._cancels.add(queue)

        async def signals() -> AsyncIterator[uuid.UUID]:
            try:
                while True:
                    yield await queue.get()
            finally:
                self._cancels.discard(queue)

        return signals()

    async def wait_for_events(
        self, owner: uuid.UUID, session: uuid.UUID, turn: uuid.UUID, after: int, timeout: float
    ) -> bool:
        def ready() -> bool:
            found = self._turns.get(turn)
            if found is None or found.state is not TurnState.RUNNING:
                return True
            return any(e.position > after for e in self._events.get(turn, ()))

        async with self._changed:
            if ready():
                return True
            try:
                await asyncio.wait_for(self._changed.wait_for(ready), timeout)
            except TimeoutError:
                return ready()
            return True
