# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The work loop of a pod: the heartbeat that keeps the turns it runs its own.

Every ``heartbeat_seconds`` it renews, in one write, the lease of every turn the dispatcher
holds, and stops the runner of each turn that write did not renew: that turn is no longer
this pod's, and its runner writes nothing more. A heartbeat that fails is logged and tried
again at the next tick; a pod that cannot reach the database loses its turns when their
leases pass, which is what a pod that died does too.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from robinauts.controller.contract.domain import WorkConfig
from robinauts.controller.ports.dispatcher import LOST, TurnDispatcher
from robinauts.controller.ports.work import HeartbeatResult, WorkQueue

log = logging.getLogger(__name__)


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
        self._beating: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._beating is None:
            self._beating = asyncio.create_task(self._beat_for_ever())

    async def stop(self) -> None:
        beating, self._beating = self._beating, None
        if beating is not None:
            beating.cancel()
            await asyncio.wait({beating})

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
        return result

    async def _beat_for_ever(self) -> None:
        while True:
            await asyncio.sleep(self._work.heartbeat_seconds)
            try:
                await self.beat()
            except Exception:
                log.exception(
                    "the heartbeat failed; it is tried again in %ss", self._work.heartbeat_seconds
                )
