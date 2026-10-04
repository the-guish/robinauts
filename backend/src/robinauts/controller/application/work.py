# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The work loop of a pod: the heartbeat that keeps the turns it runs its own, and the cancels
asked for from any pod.

Every ``heartbeat_seconds`` it renews, in one write, the lease of every turn the dispatcher
holds, and stops the runner of each turn that write did not renew: that turn is no longer
this pod's, and its runner writes nothing more. A heartbeat that fails is logged and tried
again at the next tick; a pod that cannot reach the database loses its turns when their
leases pass, which is what a pod that died does too.

A cancel asked for on any pod reaches this one as a signal, and the runner of a turn it holds
is stopped at once. A signal can be missed, with a listener that dropped; the heartbeat reads
every cancel back, so a missed one waits at most until the next tick.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta

from robinauts.controller.contract.domain import WorkConfig
from robinauts.controller.ports.dispatcher import CANCEL, LOST, TurnDispatcher
from robinauts.controller.ports.work import HeartbeatResult, Held, WorkQueue

log = logging.getLogger(__name__)

FOLLOW_AGAIN = 1.0
"""Seconds before the cancels are followed again after the signals failed."""


class WorkLoop:
    def __init__(
        self,
        queue: WorkQueue,
        dispatcher: TurnDispatcher,
        *,
        worker_id: str,
        work: WorkConfig,
        now: Callable[[], datetime],
    ) -> None:
        self._queue = queue
        self._dispatcher = dispatcher
        self._worker_id = worker_id
        self._work = work
        self._now = now
        self._loops: list[asyncio.Task[None]] = []
        self._stopping: set[asyncio.Task[None]] = set()

    def start(self) -> None:
        if not self._loops:
            signals = self._queue.cancel_signals()
            self._loops = [
                asyncio.create_task(self._beat_for_ever()),
                asyncio.create_task(self._follow_cancels(signals)),
            ]

    async def stop(self) -> None:
        loops, self._loops = self._loops, []
        for loop in loops:
            loop.cancel()
        if loops:
            await asyncio.wait(loops)
        if self._stopping:
            await asyncio.wait(self._stopping)

    def _cancel(self, turn: uuid.UUID) -> None:
        """Stop the turn's runner, if this pod runs it, without waiting here for it to end."""
        held = next((h for h in self._dispatcher.held() if h.turn_id == turn), None)
        if held is None:
            return
        stopping = asyncio.create_task(self._cancelled(held))
        self._stopping.add(stopping)
        stopping.add_done_callback(self._stopping.discard)

    async def _cancelled(self, held: Held) -> None:
        if await self._dispatcher.stop(held.turn_id, CANCEL):
            # A runner stopped before it began wrote no end of its own: it is written here.
            await self._queue.end_cancelled(self._worker_id, held, self._now())

    async def beat(self) -> HeartbeatResult:
        """One heartbeat, now: the leases renewed, and the runners of lost turns stopped."""
        held = self._dispatcher.held()
        if not held:
            return HeartbeatResult()
        result = await self._queue.heartbeat(
            self._worker_id, held, self._now(), timedelta(seconds=self._work.lease_seconds)
        )
        for turn in result.lost:
            log.warning("turn %s is no longer this pod's: its runner stops", turn)
            await self._dispatcher.stop(turn, LOST)
        for turn in result.cancelled - result.lost:
            self._cancel(turn)
        return result

    async def _follow_cancels(self, signals: AsyncIterator[uuid.UUID]) -> None:
        while True:
            try:
                async for turn in signals:
                    self._cancel(turn)
            except Exception:
                log.exception("the cancel signals failed; they are followed again")
                await asyncio.sleep(FOLLOW_AGAIN)
            signals = self._queue.cancel_signals()

    async def _beat_for_ever(self) -> None:
        while True:
            await asyncio.sleep(self._work.heartbeat_seconds)
            try:
                await self.beat()
            except Exception:
                log.exception(
                    "the heartbeat failed; it is tried again in %ss", self._work.heartbeat_seconds
                )
