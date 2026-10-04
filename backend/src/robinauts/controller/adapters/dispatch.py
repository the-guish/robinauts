# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The turn dispatcher that runs each turn as an asyncio task in this process."""

from __future__ import annotations

import asyncio
import uuid

from robinauts.controller.ports.dispatcher import CLOSE, TurnDispatcher, TurnRunner


class InProcessDispatcher(TurnDispatcher):
    def __init__(self, run: TurnRunner | None = None) -> None:
        self.run = run
        """The controller's ``run_turn``, handed over by the composition."""
        self._tasks: dict[uuid.UUID, asyncio.Task[None]] = {}

    async def dispatch(self, owner: uuid.UUID, session: uuid.UUID, turn: uuid.UUID) -> None:
        if self.run is None:
            raise RuntimeError("the dispatcher has nothing to run turns with")
        task = asyncio.create_task(self.run(owner, session, turn))
        self._tasks[turn] = task
        task.add_done_callback(lambda done: self._settled(turn, done))

    def _settled(self, turn: uuid.UUID, task: asyncio.Task[None]) -> None:
        self._tasks.pop(turn, None)
        if not task.cancelled():
            task.exception()

    async def cancel(self, owner: uuid.UUID, session: uuid.UUID, turn: uuid.UUID) -> bool:
        task = self._tasks.get(turn)
        if task is None:
            return False
        task.cancel()
        await asyncio.wait({task})
        return True

    def running(self) -> list[uuid.UUID]:
        return list(self._tasks)

    async def close(self, timeout: float) -> None:
        tasks = set(self._tasks.values())
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in pending:
            task.cancel(CLOSE)
        if pending:
            await asyncio.wait(pending)
