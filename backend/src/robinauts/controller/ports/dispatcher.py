# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The turn dispatcher: where a turn runs, in this process or in another."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

CLOSE = "close"
"""The reason a closing dispatcher cancels a turn with. The runner ends such a turn as
``interrupted``, the deployment having stopped with the turn in it, not ``cancelled``."""

TurnRunner = Callable[[uuid.UUID, uuid.UUID, uuid.UUID], Awaitable[None]]
"""What a dispatcher runs: the controller's ``run_turn``, given the owner, the session and
the turn, which loads everything else by those ids."""


class TurnDispatcher(ABC):
    @abstractmethod
    async def dispatch(self, owner: uuid.UUID, session: uuid.UUID, turn: uuid.UUID) -> None:
        """Run the turn, somewhere, and return at once."""

    @abstractmethod
    async def cancel(self, owner: uuid.UUID, session: uuid.UUID, turn: uuid.UUID) -> bool:
        """Cancel the turn if this process runs it, and wait for it to end: true when it did,
        false when the turn is not this process's."""

    @abstractmethod
    async def close(self, timeout: float) -> None:
        """Wait up to ``timeout`` seconds for the turns this process runs, then cancel the rest
        naming ``CLOSE``, and wait for those too."""
