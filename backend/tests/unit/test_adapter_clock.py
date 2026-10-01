# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The system clock: aware, in UTC, and monotonic where it says it is."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from robinauts.legacy.adapters import SystemClock
from robinauts.legacy.ports import Clock


def test_it_is_a_clock() -> None:
    # The port is an ABC: an implementation that drifted from it would not
    # instantiate at all.
    assert isinstance(SystemClock(), Clock)


def test_now_is_timezone_aware() -> None:
    # Naive is what the domain refuses outright, and for good reason: an
    # expiry read as local time by one comparison and as UTC by the next is
    # no expiry.
    now = SystemClock().now()

    assert now.tzinfo is not None
    assert now.utcoffset() is not None


def test_now_is_in_utc_whatever_the_machine_is_set_to() -> None:
    assert SystemClock().now().utcoffset() == timedelta(0)


def test_now_is_about_now() -> None:
    # Wide on purpose: this is here to catch a clock reading the wrong epoch
    # or the wrong unit, not to measure anything.
    assert abs(SystemClock().now() - datetime.now(UTC)) < timedelta(seconds=10)


def test_monotonic_never_goes_backwards() -> None:
    clock = SystemClock()

    readings = [clock.monotonic() for _ in range(100)]

    assert readings == sorted(readings)


def test_monotonic_is_not_the_wall_clock() -> None:
    # It counts from an arbitrary point, so it must not be a Unix timestamp:
    # a monotonic clock that was one would be stepped by NTP with the other.
    assert SystemClock().monotonic() < datetime.now(UTC).timestamp()
