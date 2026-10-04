# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The work loop: what a process does for the turns it runs, apart from running them.

Every ``heartbeat_seconds`` it renews the leases of every turn the dispatcher holds, in one
write, and stops, naming ``LOST``, each turn the write says this process holds no more: ended
by a reader, or past its lease. A beat that fails is logged and the next one tries again; a
lease that passes meanwhile makes its turn lost, and its runner's next write is refused.

It also stops, naming ``CANCEL``, each turn it holds whose cancel is signalled, from whichever
process the person's Stop or delete reached. A signal lost on the way is read back by the
next beat, which names every renewed turn whose cancel was asked for.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta

from robinauts.controller.ports.dispatcher import CANCEL, LOST, TurnDispatcher
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
                asyncio.create_task(self._beating(), name="robinauts-heartbeat"),
                asyncio.create_task(self._listening(), name="robinauts-cancels"),
            ]

    async def stop(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks:
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
        for turn in result.cancelled:
            self._cancel(turn)
        await asyncio.gather(*(self._dispatcher.stop(turn, LOST) for turn in result.lost))

    def _cancel(self, turn: uuid.UUID) -> None:
        """Stop the turn for its cancel, if this process holds it, without waiting here."""
        if all(held.turn != turn for held in self._dispatcher.held()):
            return
        stopping = asyncio.create_task(self._dispatcher.stop(turn, CANCEL))
        self._stopping.add(stopping)
        stopping.add_done_callback(self._stopping.discard)

    async def _listening(self) -> None:
        while True:
            try:
                async for turn in self._work.cancel_signals():
                    self._cancel(turn)
            except Exception:
                log.exception("the cancel signals stopped; listening again shortly")
                await asyncio.sleep(1.0)

    async def _beating(self) -> None:
        while True:
            await asyncio.sleep(self._every)
            try:
                await self.beat()
            except Exception:
                log.exception("the heartbeat failed; the next one tries again")
