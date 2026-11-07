# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The turn broker's port: what a worker needs from the core, and all it reaches of it."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from datetime import datetime

from robinauts.controller.contract.broker.domain import (
    Cursor,
    Document,
    StoredEvent,
    StoredMessage,
    Task,
)
from robinauts.shared_domain.domain import Session, Turn, TurnState


class TurnBroker(ABC):
    """What a worker needs from the core, and all it reaches of it: the tasks it claims, renews
    and is asked to stop, the turn each names, the session and the messages a turn reads, and
    the events and the end it writes (``docs/architecture/controller.md``, "Worker").

    The operations are the store's of the same names, with the same refusals:
    ``SessionNotFoundError`` for a session missing, hidden or another owner's, and
    ``TurnLostError`` for a write on a turn the worker no longer holds. Every time comes from
    the worker's clock. A worker in this process has it over the controller's store; one in a
    process of its own, over HTTP, from the server that spawned it (``web/worker_api.py``).

    A broker is one worker's: the tasks it claims and waits for are those of the agents that
    worker runs (``AgentConfig.worker``), and no other worker's.
    """

    @abstractmethod
    async def claim_tasks(self, now: datetime, until: datetime, limit: int) -> list[Task]:
        """Up to ``limit`` of this worker's queued tasks, made running, with their lease until
        ``until``. Each task is claimed once, whichever process of the worker asks."""
        raise NotImplementedError

    @abstractmethod
    async def wait_for_queued(self, now: datetime, timeout: float) -> bool:
        """Wait until a task ``claim_tasks`` would claim is queued, or ``timeout`` seconds
        have passed: true in the first case, false in the second."""
        raise NotImplementedError

    @abstractmethod
    async def renew_leases(
        self, tasks: Sequence[uuid.UUID], now: datetime, until: datetime
    ) -> list[uuid.UUID]:
        """Each of those tasks that is running, with its lease not passed ``now``, holds it
        until ``until``. The ones asked to stop, which a stop missed."""
        raise NotImplementedError

    @abstractmethod
    async def listen_for_cancels(self, stop: Callable[[uuid.UUID], object]) -> None:
        """From then on, call ``stop`` with each task asked to stop, from any process, until
        ``close``. A stop that goes astray is caught by the next ``renew_leases``."""
        raise NotImplementedError

    @abstractmethod
    async def get_task(self, task: uuid.UUID) -> Task | None:
        raise NotImplementedError

    @abstractmethod
    async def find_turn(self, turn: uuid.UUID) -> tuple[uuid.UUID, Turn] | None:
        """The turn of a visible session, by its id alone, with the session's owner: what a
        task names."""
        raise NotImplementedError

    @abstractmethod
    async def get_session(self, owner: uuid.UUID, session: uuid.UUID) -> Session:
        raise NotImplementedError

    @abstractmethod
    async def sessions_of(
        self, owner: uuid.UUID, limit: int, before: Cursor | None
    ) -> list[Session]:
        """A page of the owner's visible sessions, most recently updated first, past
        ``before``: what a built-in tool reads."""
        raise NotImplementedError

    @abstractmethod
    async def messages_of(self, owner: uuid.UUID, session: uuid.UUID) -> list[Document]:
        """Every message of the session, oldest first."""
        raise NotImplementedError

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
        """The same document again at a position it has is accepted; anything else there, or
        on a turn the worker does not hold, is ``TurnLostError``."""
        raise NotImplementedError

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
        """The answer, the last events, the turn's state and its task done, in one operation,
        only while the worker holds the turn: else ``TurnLostError``."""
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None:
        """Stop listening for cancels, and release what reaching the core took."""
        raise NotImplementedError
