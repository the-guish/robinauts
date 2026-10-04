# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The work a pod holds: the leases of the turns it runs, renewed by a heartbeat.

A held turn is the pod's while its lease lasts. The heartbeat renews every lease the pod
holds in one write, on a connection of its own, so that a busy pool never delays it; a turn
missing from what it renewed is lost, to a reader that ended it or to its own lease, and its
runner stops and writes nothing more.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass(frozen=True, slots=True)
class Held:
    """A turn a pod runs, and which run of it."""

    turn_id: uuid.UUID
    attempt: int


@dataclass(frozen=True, slots=True)
class HeartbeatResult:
    lost: frozenset[uuid.UUID] = field(default_factory=frozenset)
    """Held turns that are no longer this pod's: their runners stop and write nothing more."""
    cancelled: frozenset[uuid.UUID] = field(default_factory=frozenset)
    """Held turns whose cancel was asked for, from any pod."""


class WorkQueue(ABC):
    @abstractmethod
    async def heartbeat(
        self, worker: str, held: Sequence[Held], now: datetime, lease: timedelta
    ) -> HeartbeatResult:
        """Renew to ``now + lease`` the lease of each turn held that is still running, still
        that worker's at that attempt, and whose lease has not passed ``now``, and set its
        ``heartbeat_at``, in one operation. The others are lost."""

    @abstractmethod
    async def end_cancelled(self, worker: str, held: Held, now: datetime) -> bool:
        """End as ``cancelled`` a turn still running and still that worker's at that attempt,
        whose runner was stopped before it could end it: true when it did."""

    @abstractmethod
    def cancel_signals(self) -> AsyncIterator[uuid.UUID]:
        """The turns whose cancel is asked for from the moment this is called, from any pod,
        as they are asked for. A hint, which can be missed: the heartbeat reads every cancel
        back."""
