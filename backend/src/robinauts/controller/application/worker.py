# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The worker: it claims queued turns from the store and hands them to the dispatcher.

Every process runs one. A turn is claimed by one worker, whichever process stored it. The
claim makes the turn running, with a fresh lease, which the dispatching process renews.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from robinauts.controller.contract.domain import WorkConfig
from robinauts.controller.ports.dispatcher import TurnDispatcher
from robinauts.controller.ports.store import Store

_log = logging.getLogger(__name__)

QUEUE_WAIT_TIMEOUT = 5.0
"""How long the worker waits to hear of a queued turn before it looks again, in case the
announcement was lost."""

CLAIM_LIMIT = 10
"""How many turns one claim takes at most."""


class Worker:
    def __init__(
        self,
        store: Store,
        dispatcher: TurnDispatcher,
        work: WorkConfig,
        *,
        wait_timeout: float = QUEUE_WAIT_TIMEOUT,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._dispatcher = dispatcher
        self._work = work
        self._wait_timeout = wait_timeout
        self._now = now or (lambda: datetime.now(UTC))
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start claiming, on the store the controller opened."""
        self._task = asyncio.create_task(self._loop(), name="worker")

    async def stop(self) -> None:
        """Stop claiming. The turns already dispatched keep running."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def dispatch_queued(self) -> None:
        """Claim the queued turns and dispatch each, until none is left."""
        while True:
            now = self._now()
            until = now + timedelta(seconds=self._work.lease_seconds)
            claimed = await self._store.claim_turns(now, until, CLAIM_LIMIT)
            for owner, turn in claimed:
                await self._dispatcher.dispatch(owner, turn.session_id, turn.id)
            if len(claimed) < CLAIM_LIMIT:
                return

    async def _loop(self) -> None:
        while True:
            try:
                await self.dispatch_queued()
                await self._store.wait_for_queued(self._now(), self._wait_timeout)
            except Exception:
                _log.exception("the worker could not claim turns")
                await asyncio.sleep(self._wait_timeout)
