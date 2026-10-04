# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The turn dispatcher: where a turn runs. Today, in the process that started it."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from enum import StrEnum

from robinauts.controller.ports.work import Held


class StopReason(StrEnum):
    """Why a dispatcher stops a turn it runs: the message its task is cancelled with, which
    the runner reads to end the turn as it should."""

    CANCEL = "cancel"
    """Somebody asked: the runner ends the turn ``cancelled``."""
    LOST = "lost"
    """The turn is no longer this runner's to write: the runner writes nothing more."""
    CLOSE = "close"
    """The process is stopping with the turn in it: the runner ends it ``interrupted``."""


CLOSE = StopReason.CLOSE

TurnRunner = Callable[[uuid.UUID, uuid.UUID, uuid.UUID, int], Awaitable[None]]
"""What a dispatcher runs: the controller's ``run_turn``, given the owner, the session, the
turn and the attempt it holds, which loads everything else by those ids."""


class TurnDispatcher(ABC):
    @abstractmethod
    async def dispatch(
        self, owner: uuid.UUID, session: uuid.UUID, turn: uuid.UUID, attempt: int
    ) -> None:
        """Run the turn's attempt, somewhere, and return at once."""

    @abstractmethod
    def held(self) -> list[Held]:
        """The turns this process runs, each with the attempt it holds: what its heartbeat
        renews."""

    @abstractmethod
    async def stop(self, turn: uuid.UUID, reason: StopReason) -> bool:
        """Stop the turn if this process runs it, and wait for it to end: true when it did,
        false when the turn is not this process's."""

    @abstractmethod
    async def close(self, timeout: float) -> None:
        """Wait up to ``timeout`` seconds for the turns this process runs, then stop the rest
        naming ``CLOSE``, and wait for those too."""
