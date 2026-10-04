# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The housekeeper: the sweep, run by every process on an interval.

Every process sweeps, so that the deployment needs no process of its own for it, and none
that must be the one. Each task of the sweep is done by one process at a time where the
store can say so, and is idempotent all the same. The first sweep runs a little after the
start, at a moment of its own in each process, so that a fleet restarting together does
not sweep together.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)


class Housekeeper:
    def __init__(self, sweep: Callable[[], Awaitable[None]], *, every: float) -> None:
        self._sweep = sweep
        self._every = every
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._sweeping(), name="robinauts-sweep")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _sweeping(self) -> None:
        await asyncio.sleep(random.uniform(0.1, 0.3) * self._every)
        while True:
            try:
                await self._sweep()
            except Exception:
                log.exception("the sweep failed; the next one tries again")
            await asyncio.sleep(self._every)
