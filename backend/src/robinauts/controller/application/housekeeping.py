# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Housekeeping, in every pod: the sweep on an interval, and the turns left behind more often.

Every pod runs it; the store lets one pod at a time do each task and the others skip it, so
running it everywhere costs a few cheap statements and needs no leader. Every task is safe to
repeat. A sweep that fails is logged and tried again at the next tick.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)

SWEEP_SECONDS = 300.0
"""How often the whole sweep runs: what expired, and purges that never finished."""


class Housekeeper:
    def __init__(
        self,
        sweep: Callable[[], Awaitable[None]],
        end_left_turns: Callable[[], Awaitable[int]],
        *,
        every: float,
        sweep_every: float = SWEEP_SECONDS,
    ) -> None:
        self._sweep = sweep
        self._end_left_turns = end_left_turns
        self._every = every
        self._sweep_every = sweep_every
        self._loops: list[asyncio.Task[None]] = []

    def start(self) -> None:
        if not self._loops:
            self._loops = [
                asyncio.create_task(self._for_ever(self._end_left_turns, self._every)),
                asyncio.create_task(self._for_ever(self._sweep, self._sweep_every)),
            ]

    async def stop(self) -> None:
        loops, self._loops = self._loops, []
        for loop in loops:
            loop.cancel()
        if loops:
            await asyncio.wait(loops)

    @staticmethod
    async def _for_ever(task: Callable[[], Awaitable[object]], every: float) -> None:
        while True:
            await asyncio.sleep(every)
            try:
                await task()
            except Exception:
                log.exception("housekeeping failed; it is tried again in %ss", every)
