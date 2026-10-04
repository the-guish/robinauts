# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The work a process does for the turns it holds, apart from the store's records: the
heartbeat that renews their leases.

A turn is held by one process, its ``worker_id``, under an ``attempt``: every write of its
runner names both, and the store refuses one whose turn another holds, or holds under
another attempt, or whose lease has passed (``Fence``). The holder renews the leases of
every turn it runs in one operation per heartbeat. A turn missing from what a heartbeat
renewed is lost: it ended, or is another's, or its lease passed before it was renewed.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True, slots=True)
class Held:
    """A turn this process runs, and the attempt it holds it under."""

    turn: uuid.UUID
    attempt: int


@dataclass(frozen=True, slots=True)
class Fence:
    """Who writes for a turn: the process holding it, and under which attempt."""

    worker: str
    attempt: int


@dataclass(frozen=True, slots=True)
class Renewed:
    """What a heartbeat renewed: each turn with when a cancel of it was asked for, if one
    was."""

    turns: Mapping[uuid.UUID, datetime | None]

    def lost(self, held: Sequence[Held]) -> list[uuid.UUID]:
        """The turns held that were not renewed."""
        return [h.turn for h in held if h.turn not in self.turns]


class WorkQueue(ABC):
    @abstractmethod
    async def heartbeat(
        self, worker: str, held: Sequence[Held], now: datetime, lease: timedelta
    ) -> Renewed:
        """Renew the lease of every turn held to ``now + lease``, in one operation, and only
        while the turn is running, held by ``worker`` under that attempt, and its lease has
        not passed ``now``: a lease that has passed is never renewed."""
