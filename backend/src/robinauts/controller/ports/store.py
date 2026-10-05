# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What the controller keeps: users, sessions, their messages, turns and each turn's events.

The model is ``docs/architecture/data-model.md``. Every operation on a session names its
owner and the session; every operation on a turn names the owner, the session and the turn,
so that a store keeping a session's records in one partition finds them from those ids. A
session that is not that owner's, or is hidden, is ``SessionNotFoundError``. Messages and
events cross as the documents ``controller.core.documents`` writes, with the columns
beside them, and the store never reads inside one. A store keeps no clock and mints no id:
every time and every id comes from the caller.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from robinauts.controller.contract.domain import Role, Session, Task, Turn, TurnState, User

Document = Mapping[str, Any]
"""A message or an event, whole, as the encoder wrote it."""

Cursor = tuple[datetime, uuid.UUID]
"""Where a page of sessions ended: the ``(updated_at, id)`` of its last session."""


@dataclass(frozen=True, slots=True)
class StoredMessage:
    """A message as a store keeps it: the columns it orders and joins by, and the document."""

    id: uuid.UUID
    session_id: uuid.UUID
    parent_id: uuid.UUID | None
    role: Role
    created_at: datetime
    document: Document


@dataclass(frozen=True, slots=True)
class StoredEvent:
    """One of a turn's events: its position, its document, and when it expires."""

    position: int
    document: Document
    expires_at: datetime


class Store(ABC):
    @abstractmethod
    async def open(self) -> object | None:
        """Open what the store needs, if anything, and hand back what the engines take as
        their storage's handle: a PostgreSQL pool, or ``None``."""
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None:
        """Release what ``open`` took."""
        raise NotImplementedError

    # --- users --------------------------------------------------------------

    @abstractmethod
    async def add_user_if_absent(self, user: User) -> User:
        """The user stored for that identity: ``user`` if there was none, else the one there."""

    # --- sessions -----------------------------------------------------------

    @abstractmethod
    async def add_session(self, session: Session) -> None: ...

    @abstractmethod
    async def get_session(self, owner: uuid.UUID, session: uuid.UUID) -> Session:
        """``SessionNotFoundError`` for a session that is missing, hidden or another owner's."""

    @abstractmethod
    async def update_session(self, session: Session) -> None: ...

    @abstractmethod
    async def sessions_of(
        self, owner: uuid.UUID, limit: int, before: Cursor | None
    ) -> list[Session]:
        """A page of the owner's visible sessions, most recently updated first, ties broken
        by id, past ``before``."""

    @abstractmethod
    async def hide_session(self, owner: uuid.UUID, session: uuid.UUID, at: datetime) -> None:
        """From then on the session is not found, even while a turn runs: that turn's next
        write is refused."""

    @abstractmethod
    async def purge_session(self, owner: uuid.UUID, session: uuid.UUID, now: datetime) -> bool:
        """Delete the session with its messages, turns, their tasks and events, unless an active
        turn of it has a task whose lease has not passed ``now``: true when it did, false also
        if it is gone."""

    @abstractmethod
    async def messages_of(self, owner: uuid.UUID, session: uuid.UUID) -> list[Document]:
        """Every message of the session, oldest first, ties broken by id."""

    # --- turns --------------------------------------------------------------

    @abstractmethod
    async def queue_turn(
        self, owner: uuid.UUID, turn: Turn, task: Task, question: StoredMessage | None
    ) -> None:
        """Store the question, when there is one, the task and the turn, or none of them:
        ``TurnActiveError`` while the session has an active turn. A queued task wakes every
        process waiting in ``wait_for_queued``."""

    @abstractmethod
    async def claim_tasks(self, now: datetime, until: datetime, limit: int) -> list[Task]:
        """Up to ``limit`` queued tasks whose lease has not passed ``now``, oldest first, made
        running, claimed at ``now`` with their lease until ``until``; in one operation. Each
        task is claimed once, whichever process asks."""

    @abstractmethod
    async def wait_for_queued(self, now: datetime, timeout: float) -> bool:
        """Wait until a task ``claim_tasks`` would claim is queued, or ``timeout`` seconds
        have passed: true in the first case, false in the second."""

    @abstractmethod
    async def get_task(self, task: uuid.UUID) -> Task | None: ...

    @abstractmethod
    async def find_turn(self, turn: uuid.UUID) -> tuple[uuid.UUID, Turn] | None:
        """The turn of a visible session, by its id alone, with the session's owner: what a
        task names."""

    @abstractmethod
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
        """The same document again at a position it has is accepted. Another document there,
        or any append on a turn that is not active, or whose task is not running or has a lease
        passed ``written_at``, is ``TurnLostError``."""

    @abstractmethod
    async def events_after(
        self, owner: uuid.UUID, session: uuid.UUID, turn: uuid.UUID, position: int
    ) -> list[tuple[int, Document]]:
        """The turn's events past ``position``, in order."""

    @abstractmethod
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
        """The answer, the last events, the turn's state, its task done and the session's
        ``updated_at``, in one operation, only while the turn is active and its task runs with
        a lease not passed ``ended_at``: else ``TurnLostError``."""

    @abstractmethod
    async def end_expired_turn(
        self,
        owner: uuid.UUID,
        session: uuid.UUID,
        turn: uuid.UUID,
        now: datetime,
        answer: StoredMessage | None = None,
    ) -> Turn | None:
        """The turn ended as ``interrupted`` if it is active and its task's lease has passed
        ``now``, with ``answer`` stored and the task done, in one operation, with no event, and
        its watchers woken; ``None`` otherwise. A queued task's lease is how long it may wait."""

    @abstractmethod
    async def renew_leases(
        self, tasks: Sequence[uuid.UUID], now: datetime, until: datetime
    ) -> list[uuid.UUID]:
        """Each of those tasks that is running, with its lease not passed ``now``, holds it
        until ``until``; in one operation. The ones asked to stop, which a stop missed."""

    @abstractmethod
    async def request_cancel(
        self, owner: uuid.UUID, session: uuid.UUID, turn: uuid.UUID, at: datetime
    ) -> None:
        """If the turn is active: with its task queued, end both, the turn as ``cancelled``,
        and wake its watchers; with its task running, mark the task as asked to stop, and tell
        every process that listens with ``listen_for_cancels``."""

    @abstractmethod
    async def listen_for_cancels(self, stop: Callable[[uuid.UUID], object]) -> None:
        """From then on, call ``stop`` with each task asked to stop, from any process."""

    @abstractmethod
    async def hidden_sessions(self, now: datetime) -> list[Session]:
        """Every hidden session with no active turn whose task's lease has not passed
        ``now``: what is left to purge."""

    @abstractmethod
    async def expired_tasks(self, now: datetime) -> list[Task]:
        """Every task not done whose lease has passed ``now``, of any kind."""

    @abstractmethod
    async def end_expired_task(self, task: uuid.UUID, now: datetime) -> bool:
        """The task done, if it is not and its lease has passed ``now``: true when it was. A
        turn of it is the caller's to end, with ``end_expired_turn``."""

    @abstractmethod
    async def active_turn(self, owner: uuid.UUID, session: uuid.UUID) -> Turn | None:
        """The session's active turn."""

    @abstractmethod
    async def latest_turn(self, owner: uuid.UUID, session: uuid.UUID) -> Turn | None:
        """The session's most recently started turn, active or not."""

    @abstractmethod
    async def get_turn(
        self, owner: uuid.UUID, session: uuid.UUID, turn: uuid.UUID
    ) -> Turn | None: ...

    @abstractmethod
    async def wait_for_events(
        self, owner: uuid.UUID, session: uuid.UUID, turn: uuid.UUID, after: int, timeout: float
    ) -> bool:
        """Wait until the turn has an event past ``after`` or has ended, or ``timeout``
        seconds have passed: true in the first two cases, false in the last. The caller reads
        the store again in every case."""
