# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The work loop: this process's heartbeat over the turns it runs.

Every ``heartbeat_seconds`` it renews the lease of every turn the dispatcher holds, in one
write. A turn the write did not renew is no longer this process's, and its runner is stopped
naming ``LOST``: it writes nothing more. A heartbeat that fails is logged and tried again at
the next tick; the leases it did not renew run out, and so do the turns.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from robinauts.controller.ports.dispatcher import StopReason, TurnDispatcher
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
            self._task = asyncio.create_task(self._run(), name="robinauts-heartbeat")

    async def stop(self) -> None:
        if self._task is not None:
            task, self._task = self._task, None
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def beat(self) -> None:
        """One heartbeat: renew what this process holds, and stop what it lost."""
        held = self._dispatcher.held()
        if not held:
            return
        renewed = await self._work.heartbeat(self._worker, held, self._now(), self._lease)
        # A turn that ended while the heartbeat was on its way is not renewed either, and
        # has left the dispatcher by now: only the ones still run here are lost.
        still = set(self._dispatcher.held())
        for turn in renewed.lost([h for h in held if h in still]):
            if await self._dispatcher.stop(turn, StopReason.LOST):
                log.warning("turn %s is no longer this process's: its lease was not renewed", turn)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._every)
            try:
                await self.beat()
            except Exception:
                log.exception("the heartbeat failed; the leases it did not renew run on")
