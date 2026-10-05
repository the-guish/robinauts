# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The store over dicts, in this process, keeping the model of ``data-model.md`` by the same
keys a partitioned store would."""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime

from robinauts.controller.contract.domain import (
    Session,
    SessionNotFoundError,
    Task,
    TaskState,
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


class MemoryStore(Store):
    def __init__(self) -> None:
        self._users: dict[tuple[str, str], User] = {}
        self._sessions: dict[uuid.UUID, Session] = {}
        self._hidden: dict[uuid.UUID, datetime] = {}
        self._messages: dict[uuid.UUID, list[StoredMessage]] = {}
        self._turns: dict[uuid.UUID, Turn] = {}
        self._tasks: dict[uuid.UUID, Task] = {}
        self._events: dict[uuid.UUID, list[StoredEvent]] = {}
        self._changed = asyncio.Condition()
        self._cancel_requested: set[uuid.UUID] = set()
        self._stops: list[Callable[[uuid.UUID], object]] = []

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

    def _active(self, session: uuid.UUID) -> Turn | None:
        return next(
            (
                t
                for t in self._turns.values()
                if t.session_id == session and t.state is TurnState.ACTIVE
            ),
            None,
        )

    def _claimable(self, now: datetime) -> list[Task]:
        queued = [
            k for k in self._tasks.values() if k.state is TaskState.QUEUED and k.lease_until > now
        ]
        return sorted(queued, key=lambda k: (k.created_at, k.id))

    def _holds(self, turn: Turn | None, at: datetime) -> bool:
        """Whether the turn is active and its task runs with a lease not passed ``at``."""
        if turn is None or turn.state is not TurnState.ACTIVE:
            return False
        task = self._tasks[turn.task_id]
        return task.state is TaskState.RUNNING and task.lease_until > at

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
        self._hidden[session] = at
        await self._notify()

    async def purge_session(self, owner: uuid.UUID, session: uuid.UUID, now: datetime) -> bool:
        found = self._sessions.get(session)
        if found is None or found.owner_id != owner or self._live(session, now):
            return False
        del self._sessions[session]
        self._hidden.pop(session, None)
        self._messages.pop(session, None)
        for turn in [t for t in self._turns.values() if t.session_id == session]:
            del self._turns[turn.id]
            self._tasks.pop(turn.task_id, None)
            self._events.pop(turn.id, None)
            self._cancel_requested.discard(turn.task_id)
        await self._notify()
        return True

    async def messages_of(self, owner: uuid.UUID, session: uuid.UUID) -> list[Document]:
        self._visible(owner, session)
        stored = sorted(self._messages[session], key=lambda m: (m.created_at, m.id))
        return [m.document for m in stored]

    # --- turns --------------------------------------------------------------

    async def queue_turn(
        self, owner: uuid.UUID, turn: Turn, task: Task, question: StoredMessage | None
    ) -> None:
        self._visible(owner, turn.session_id)
        if self._active(turn.session_id) is not None:
            raise TurnActiveError(str(turn.session_id))
        if question is not None:
            self._messages[turn.session_id].append(question)
        self._tasks[task.id] = task
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
    ) -> None:
        self._visible(owner, session)
        if not self._holds(self._turn_of(session, turn), written_at):
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
    ) -> None:
        self._visible(owner, session)
        found = self._turn_of(session, turn)
        if found is None or not self._holds(found, ended_at):
            raise TurnLostError(f"turn {turn} is not running")
        self._tasks[found.task_id] = dataclasses.replace(
            self._tasks[found.task_id], state=TaskState.DONE
        )
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
        turn: uuid.UUID,
        now: datetime,
        answer: StoredMessage | None = None,
    ) -> Turn | None:
        self._visible(owner, session)
        active = self._active(session)
        if active is None or active.id != turn:
            return None
        task = self._tasks[active.task_id]
        if task.state is TaskState.DONE or task.lease_until >= now:
            return None
        error = "queued too long" if task.state is TaskState.QUEUED else "lease expired"
        ended = dataclasses.replace(active, state=TurnState.INTERRUPTED, ended_at=now, error=error)
        self._turns[active.id] = ended
        self._tasks[task.id] = dataclasses.replace(task, state=TaskState.DONE)
        if answer is not None:
            self._messages[session].append(answer)
        await self._notify()
        return ended

    async def renew_leases(
        self, tasks: Sequence[uuid.UUID], now: datetime, until: datetime
    ) -> list[uuid.UUID]:
        renewed = []
        for task in tasks:
            found = self._tasks.get(task)
            if found is not None and found.state is TaskState.RUNNING and found.lease_until > now:
                self._tasks[task] = dataclasses.replace(found, lease_until=until)
                renewed.append(task)
        return [k for k in renewed if k in self._cancel_requested]

    async def claim_tasks(self, now: datetime, until: datetime, limit: int) -> list[Task]:
        claimed = []
        for found in self._claimable(now)[:limit]:
            running = dataclasses.replace(
                found, state=TaskState.RUNNING, lease_until=until, claimed_at=now
            )
            self._tasks[found.id] = running
            claimed.append(running)
        return claimed

    async def get_task(self, task: uuid.UUID) -> Task | None:
        return self._tasks.get(task)

    async def find_turn(self, turn: uuid.UUID) -> tuple[uuid.UUID, Turn] | None:
        found = self._turns.get(turn)
        if found is None or found.session_id in self._hidden:
            return None
        return self._sessions[found.session_id].owner_id, found

    async def wait_for_queued(self, now: datetime, timeout: float) -> bool:
        async with self._changed:
            try:
                await asyncio.wait_for(
                    self._changed.wait_for(lambda: bool(self._claimable(now))), timeout
                )
            except TimeoutError:
                return False
            return True

    async def request_cancel(
        self, owner: uuid.UUID, session: uuid.UUID, turn: uuid.UUID, at: datetime
    ) -> None:
        self._visible(owner, session)
        active = self._active(session)
        if active is None or active.id != turn:
            return
        task = self._tasks[active.task_id]
        if task.state is TaskState.QUEUED:
            # No worker holds it: both end here.
            self._tasks[task.id] = dataclasses.replace(task, state=TaskState.DONE)
            self._turns[turn] = dataclasses.replace(active, state=TurnState.CANCELLED, ended_at=at)
            await self._notify()
        elif task.state is TaskState.RUNNING:
            self._cancel_requested.add(task.id)
            for stop in self._stops:
                stop(task.id)

    async def listen_for_cancels(self, stop: Callable[[uuid.UUID], object]) -> None:
        self._stops.append(stop)

    async def hidden_sessions(self, now: datetime) -> list[Session]:
        return [self._sessions[s] for s in self._hidden if not self._live(s, now)]

    def _live(self, session: uuid.UUID, now: datetime) -> bool:
        """Whether the session has an active turn whose task's lease has not passed ``now``."""
        active = self._active(session)
        return active is not None and self._tasks[active.task_id].lease_until > now

    async def expired_tasks(self, now: datetime) -> list[Task]:
        return [
            k for k in self._tasks.values() if k.state is not TaskState.DONE and k.lease_until < now
        ]

    async def end_expired_task(self, task: uuid.UUID, now: datetime) -> bool:
        found = self._tasks.get(task)
        if found is None or found.state is TaskState.DONE or found.lease_until >= now:
            return False
        self._tasks[task] = dataclasses.replace(found, state=TaskState.DONE)
        return True

    async def active_turn(self, owner: uuid.UUID, session: uuid.UUID) -> Turn | None:
        self._visible(owner, session)
        return self._active(session)

    async def latest_turn(self, owner: uuid.UUID, session: uuid.UUID) -> Turn | None:
        self._visible(owner, session)
        turns = [t for t in self._turns.values() if t.session_id == session]
        return max(turns, key=lambda t: (t.started_at, t.id), default=None)

    async def get_turn(self, owner: uuid.UUID, session: uuid.UUID, turn: uuid.UUID) -> Turn | None:
        self._visible(owner, session)
        return self._turn_of(session, turn)

    async def wait_for_events(
        self, owner: uuid.UUID, session: uuid.UUID, turn: uuid.UUID, after: int, timeout: float
    ) -> bool:
        def ready() -> bool:
            found = self._turns.get(turn)
            if found is None or found.state is not TurnState.ACTIVE:
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
