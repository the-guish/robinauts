# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The machine's clock, behind the ``Clock`` port.

Three lines of code, and a layer of its own all the same: nothing above the
ports reads a clock directly (``docs/layout.md``), so this is where
``datetime.now`` and ``time.monotonic`` are called, and the one thing a test
replaces to make a session expire without waiting for it.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from robinauts.legacy.ports import Clock


class SystemClock(Clock):
    """What the operating system says the time is."""

    def now(self) -> datetime:
        """The current instant, in UTC and **aware**.

        In UTC rather than in the machine's own zone: an instant is what the
        database stores and what every expiry is compared against, and a
        deployment whose zone changes under it -- a container that learns its
        region, a server moved -- must not change what its rows mean. The
        zone is a display question, and nothing here displays anything.
        """
        return datetime.now(UTC)

    def monotonic(self) -> float:
        """Seconds from an arbitrary point, never stepped by an operator or NTP."""
        return time.monotonic()
