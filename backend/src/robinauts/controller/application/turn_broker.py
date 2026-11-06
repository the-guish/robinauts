# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The turn broker in this process: what a worker needs from the core, over the controller's
own store.

The worker of a server that runs it in this process has it, and so has the API that serves a
worker in a process of its own (``web/worker_api.py``). Each is one worker's, by name: it
claims the tasks of that worker's agents alone. The store is the controller's, which the
controller opens and closes: this opens and closes nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from datetime import datetime

from robinauts.controller.contract.broker.domain import (
    Cursor,
    Document,
    StoredEvent,
    StoredMessage,
    Task,
)
from robinauts.controller.contract.broker.ports import TurnBroker
from robinauts.controller.ports.store import Store
from robinauts.shared_domain.domain import DEFAULT_WORKER, Session, Turn, TurnState


class InProcessTurnBroker(TurnBroker):
    def __init__(self, store: Store, worker: str = DEFAULT_WORKER) -> None:
        self._store = store
        self.worker = worker
        """The worker whose tasks this claims and waits for."""

    async def claim_tasks(self, now: datetime, until: datetime, limit: int) -> list[Task]:
        return await self._store.claim_tasks(now, until, limit, self.worker)

    async def wait_for_queued(self, now: datetime, timeout: float) -> bool:
        return await self._store.wait_for_queued(now, timeout, self.worker)

    async def renew_leases(
        self, tasks: Sequence[uuid.UUID], now: datetime, until: datetime
    ) -> list[uuid.UUID]:
        return await self._store.renew_leases(tasks, now, until)

    async def listen_for_cancels(self, stop: Callable[[uuid.UUID], object]) -> None:
        await self._store.listen_for_cancels(stop)

    async def get_task(self, task: uuid.UUID) -> Task | None:
        return await self._store.get_task(task)

    async def find_turn(self, turn: uuid.UUID) -> tuple[uuid.UUID, Turn] | None:
        return await self._store.find_turn(turn)

    async def get_session(self, owner: uuid.UUID, session: uuid.UUID) -> Session:
        return await self._store.get_session(owner, session)

    async def sessions_of(
        self, owner: uuid.UUID, limit: int, before: Cursor | None
    ) -> list[Session]:
        return await self._store.sessions_of(owner, limit, before)

    async def messages_of(self, owner: uuid.UUID, session: uuid.UUID) -> list[Document]:
        return await self._store.messages_of(owner, session)

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
        await self._store.append_event(
            owner, session, turn, position, document, written_at, expires_at
        )

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
        await self._store.finish_turn(
            owner, session, turn, state, ended_at, error, answer, events, updated_at
        )

    async def close(self) -> None:
        """Nothing to release: the store is the controller's. A stop it listens with stays,
        and ignores the tasks its worker no longer runs."""
