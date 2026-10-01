# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What time it is, as something that can be handed to a test.

Nothing above this port reads a clock of its own: ``core`` takes ``now`` as an
argument (``docs/layout.md``), and the application asks here. A test therefore
moves time by setting a fake, rather than by sleeping.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime


class Clock(ABC):
    """The two clocks the platform needs: one that dates things, one that measures."""

    @abstractmethod
    def now(self) -> datetime:
        """The current time, as a **timezone-aware** datetime.

        Aware, always. A naive one would be read as local time by one
        comparison and as UTC by the next, and an expiry that means two things
        is no expiry; the domain's records refuse a naive datetime outright.
        """
        raise NotImplementedError

    @abstractmethod
    def monotonic(self) -> float:
        """Seconds from some fixed point, never going backwards.

        For measuring how long ago something happened -- the sweep of expired
        rows, a timeout -- where a wall clock stepped by an operator or by NTP
        would measure a negative interval, or a very long one.
        """
        raise NotImplementedError
