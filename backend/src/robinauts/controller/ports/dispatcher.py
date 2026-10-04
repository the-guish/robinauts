# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The turn dispatcher: where a turn runs, in this process or in another."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

from robinauts.controller.ports.work import Held

CLOSE = "close"
"""The reason a closing dispatcher cancels a turn with. The runner ends such a turn as
``interrupted``, the deployment having stopped with the turn in it, not ``cancelled``."""

CANCEL = "cancel"
"""The reason a turn is stopped with when somebody asked, from any pod, for it to stop. The
runner ends it as ``cancelled``."""

LOST = "lost"
"""The reason a turn whose lease the heartbeat could not renew is stopped with. The turn is
no longer this pod's, so its runner writes nothing more."""

TurnRunner = Callable[[uuid.UUID, uuid.UUID, uuid.UUID, int], Awaitable[None]]
"""What a dispatcher runs: the controller's ``run_turn``, given the owner, the session, the
turn and the attempt, which loads everything else by those ids."""


class TurnDispatcher(ABC):
    @abstractmethod
    async def dispatch(
        self, owner: uuid.UUID, session: uuid.UUID, turn: uuid.UUID, attempt: int
    ) -> None:
        """Run that attempt of the turn, somewhere, and return at once."""

    @abstractmethod
    def held(self) -> list[Held]:
        """The turns this pod runs, whose leases its heartbeat renews."""

    @abstractmethod
    async def stop(self, turn: uuid.UUID, reason: str) -> bool:
        """Cancel the turn naming ``reason`` if this pod runs it, and wait for it to end: true
        when it did, false when the turn is not this pod's."""

    @abstractmethod
    async def close(self, timeout: float) -> None:
        """Wait up to ``timeout`` seconds for the turns this process runs, then cancel the rest
        naming ``CLOSE``, and wait a few seconds more for those to write their end."""
