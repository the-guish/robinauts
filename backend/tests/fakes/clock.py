# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A clock a test sets, so that nothing has to sleep to reach an expiry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from robinauts.legacy.ports import Clock

START = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
"""Where a fake clock stands until a test moves it."""


class FakeClock(Clock):
    """Time that only a test moves.

    ``advance`` moves both clocks together, as a real pair of them moves.
    ``set`` moves the wall clock alone, which is what an operator or NTP does
    and what the monotonic clock exists to survive.
    """

    def __init__(self, now: datetime = START, monotonic: float = 1_000.0) -> None:
        self._now = now
        self._monotonic = monotonic

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, seconds: float | timedelta) -> None:
        """Move both clocks forward by the same amount."""
        step = seconds if isinstance(seconds, timedelta) else timedelta(seconds=seconds)
        self._now += step
        self._monotonic += step.total_seconds()

    def set(self, when: datetime) -> None:
        """Put the wall clock at ``when``, leaving the monotonic one where it is."""
        self._now = when
