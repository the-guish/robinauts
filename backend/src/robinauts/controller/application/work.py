# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The work loop: what a process does for the turns it runs, apart from running them.

Every ``heartbeat_seconds`` it renews the leases of every turn the dispatcher holds, in one
write, and stops, naming ``LOST``, each turn the write says this process holds no more: ended
by a reader, or past its lease. A beat that fails is logged and the next one tries again; a
lease that passes meanwhile makes its turn lost, and its runner's next write is refused.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from robinauts.controller.ports.dispatcher import LOST, TurnDispatcher
from robinauts.controller.ports.work import WorkQueue

log = logging.getLogger(__name__)


class WorkLoop:
    def __init__(
        self,
        work: WorkQueue,
        dispatcher: TurnDispatcher,
        worker: str,
        *,
        lease: timedelta,
        every: float,
        now: Callable[[], datetime],
    ) -> None:
        self._work = work
        self._dispatcher = dispatcher
        self._worker = worker
        self._lease = lease
        self._every = every
        self._now = now
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._beating(), name="robinauts-heartbeat")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def beat(self) -> None:
        """One renewal of every held turn's lease, and the stop of every turn lost."""
        held = self._dispatcher.held()
        if not held:
            return
        result = await self._work.heartbeat(self._worker, held, self._now(), self._lease)
        for turn in result.lost:
            log.warning("turn %s is this process's no more: its runner stops", turn)
        await asyncio.gather(*(self._dispatcher.stop(turn, LOST) for turn in result.lost))

    async def _beating(self) -> None:
        while True:
            await asyncio.sleep(self._every)
            try:
                await self.beat()
            except Exception:
                log.exception("the heartbeat failed; the next one tries again")
