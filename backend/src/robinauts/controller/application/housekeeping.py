# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The housekeeper: the controller's sweep, on a schedule of this process's own.

Every process runs one. The first sweep comes at a random moment of the first interval, so a
fleet started together does not sweep together. Each task takes a lock of its own while it
deletes, or while it reads what it will end or purge, and a process that finds the lock
taken leaves the task: so two processes rarely do the same work, but may, since ending and
purging come after the read. Nothing depends on the lock: every task is safe to repeat. A
sweep that fails is logged and the next one goes on, and a row that fails is skipped.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Awaitable, Callable

from robinauts.controller.contract.domain import Swept

log = logging.getLogger(__name__)


class Housekeeper:
    def __init__(self, sweep: Callable[[], Awaitable[Swept]], *, every: float) -> None:
        self._sweep = sweep
        self._every = every
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="robinauts-sweep")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        await asyncio.sleep(random.uniform(0, self._every))
        while True:
            try:
                swept = await self._sweep()
                if swept != Swept(skipped=swept.skipped):
                    log.info("swept: %s", swept)
            except Exception:
                log.exception("the sweep failed; the next one goes on")
            await asyncio.sleep(self._every)
