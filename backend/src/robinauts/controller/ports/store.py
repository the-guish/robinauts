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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from robinauts.controller.contract.domain import Role, Session, Turn, TurnState, User
from robinauts.controller.ports.work import Fence

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
        """From then on the session is not found. ``TurnActiveError`` while a turn runs."""

    @abstractmethod
    async def purge_session(self, owner: uuid.UUID, session: uuid.UUID) -> None:
        """Delete the session with its messages, turns and events; nothing if it is gone."""

    @abstractmethod
    async def messages_of(self, owner: uuid.UUID, session: uuid.UUID) -> list[Document]:
        """Every message of the session, oldest first, ties broken by id."""

    # --- turns --------------------------------------------------------------

    @abstractmethod
    async def start_turn(
        self, owner: uuid.UUID, turn: Turn, question: StoredMessage | None
    ) -> None:
        """Store the question, when there is one, and the turn, or neither:
        ``TurnActiveError`` while the session has a running turn."""

    @abstractmethod
    async def append_event(
        self,
        owner: uuid.UUID,
        session: uuid.UUID,
        turn: uuid.UUID,
        fence: Fence,
        position: int,
        document: Document,
        written_at: datetime,
        expires_at: datetime,
    ) -> None:
        """The same document again at a position it has is accepted. Another document there,
        or any append on a turn that is not running, is not held under ``fence``, or whose
        lease has passed ``written_at``, is ``TurnLostError``."""

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
        fence: Fence,
        state: TurnState,
        ended_at: datetime,
        error: str | None,
        answer: StoredMessage | None,
        events: Sequence[StoredEvent],
        updated_at: datetime,
    ) -> None:
        """The answer, the last events, the turn's state and the session's ``updated_at``, in
        one operation, only while the turn is running, held under ``fence``, and its lease
        has not passed ``ended_at``: else ``TurnLostError``."""

    @abstractmethod
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
        """End the turn as ``state``, with the answer and the last events, and the session's
        ``updated_at``, in one operation that only a running turn whose lease has passed
        ``now`` takes: the turn as ended, or ``None`` when it is not such a turn.
        ``TurnLostError`` when a position of ``events`` was taken meanwhile, by a runner
        whose clock is behind: the caller reads the events again."""

    @abstractmethod
    async def active_turn(self, owner: uuid.UUID, session: uuid.UUID) -> Turn | None: ...

    @abstractmethod
    async def latest_turn(self, owner: uuid.UUID, session: uuid.UUID) -> Turn | None:
        """The session's most recently started turn, running or not."""

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
