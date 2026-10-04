# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The work of a process that runs turns: renewing their leases, in one write per beat.

Kept apart from ``Store``: a PostgreSQL store runs it on a connection of its own, outside the
pool, so that a busy pool never delays a lease. Times come from the caller, as in the store.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass(frozen=True, slots=True)
class Held:
    """A running turn as the process holding it knows it: which, and at which attempt."""

    turn: uuid.UUID
    attempt: int


@dataclass(frozen=True, slots=True)
class HeartbeatResult:
    lost: frozenset[uuid.UUID] = field(default_factory=frozenset)
    """Turns this process held and holds no more: ended, past their lease, or another's."""


class WorkQueue(ABC):
    @abstractmethod
    async def heartbeat(
        self, worker: str, held: Sequence[Held], now: datetime, lease: timedelta
    ) -> HeartbeatResult:
        """Renew, to ``now + lease``, the lease of every turn in ``held`` that is running, is
        ``worker``'s at that attempt, and whose lease has not passed ``now``; every other is
        lost."""
