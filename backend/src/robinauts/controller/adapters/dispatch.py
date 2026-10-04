# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The turn dispatcher that runs each turn as an asyncio task in this process."""

from __future__ import annotations

import asyncio
import uuid

from robinauts.controller.ports.dispatcher import CLOSE, TurnDispatcher, TurnRunner
from robinauts.controller.ports.work import Held


class InProcessDispatcher(TurnDispatcher):
    def __init__(self, run: TurnRunner | None = None) -> None:
        self.run = run
        """The controller's ``run_turn``, handed over by the composition."""
        self._tasks: dict[uuid.UUID, tuple[asyncio.Task[None], int]] = {}

    async def dispatch(
        self, owner: uuid.UUID, session: uuid.UUID, turn: uuid.UUID, attempt: int
    ) -> None:
        if self.run is None:
            raise RuntimeError("the dispatcher has nothing to run turns with")
        task = asyncio.create_task(self.run(owner, session, turn))
        self._tasks[turn] = (task, attempt)
        task.add_done_callback(lambda done: self._settled(turn, done))

    def _settled(self, turn: uuid.UUID, task: asyncio.Task[None]) -> None:
        held = self._tasks.get(turn)
        if held is not None and held[0] is task:
            del self._tasks[turn]
        if not task.cancelled():
            task.exception()

    async def stop(self, turn: uuid.UUID, reason: str) -> bool:
        held = self._tasks.get(turn)
        if held is None:
            return False
        task = held[0]
        task.cancel(reason)
        await asyncio.wait({task})
        return True

    def held(self) -> list[Held]:
        return [Held(turn, attempt) for turn, (_, attempt) in self._tasks.items()]

    async def close(self, timeout: float) -> None:
        tasks = {task for task, _ in self._tasks.values()}
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in pending:
            task.cancel(CLOSE)
        if pending:
            await asyncio.wait(pending)
