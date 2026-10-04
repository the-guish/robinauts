# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The work loop: this process's heartbeat over the turns it runs, and their cancels.

Every ``heartbeat_seconds`` it renews the lease of every turn the dispatcher holds, in one
write. A turn the write did not renew is no longer this process's, and its runner is stopped
naming ``LOST``: it writes nothing more. A heartbeat that fails is logged and tried again at
the next tick; the leases it did not renew run out, and so do the turns.

A cancel asked for through any process arrives as a signal, and the turn is stopped naming
``CANCEL`` if this process runs it. A signal lost with a dropped connection is read back by
the next heartbeat, which returns every ask with the leases it renewed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta

from robinauts.controller.contract import logs
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
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping: set[asyncio.Task[bool]] = set()

    def start(self) -> None:
        if not self._tasks:
            self._tasks = [
                asyncio.create_task(self._run(), name="robinauts-heartbeat"),
                asyncio.create_task(self._cancels(), name="robinauts-cancels"),
            ]

    async def stop(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks:
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
                with logs.about(turn=turn):
                    log.warning("the turn is no longer this process's: its lease was not renewed")
        for turn, asked in renewed.turns.items():
            if asked is not None:
                self._cancel(turn)

    def _cancel(self, turn: uuid.UUID) -> None:
        """Stop the turn naming ``CANCEL`` if this process runs it, without waiting for it."""
        if all(h.turn != turn for h in self._dispatcher.held()):
            return
        stopping = asyncio.create_task(self._dispatcher.stop(turn, StopReason.CANCEL))
        self._stopping.add(stopping)
        stopping.add_done_callback(self._stopping.discard)

    async def _cancels(self) -> None:
        while True:
            try:
                async for turn in self._work.cancel_signals():
                    self._cancel(turn)
            except Exception:
                log.exception("the cancel signals stopped; listening again")
                await asyncio.sleep(self._every)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._every)
            try:
                await self.beat()
            except Exception:
                log.exception("the heartbeat failed; the leases it did not renew run on")
