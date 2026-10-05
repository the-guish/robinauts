# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Work in the background on a schedule: the controller's sweep, the worker's heartbeat."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

_log = logging.getLogger(__name__)


async def run_at_intervals(interval: float, func: Callable[[], Awaitable[None]]) -> None:
    """Run ``func`` every ``interval`` seconds until cancelled; a failure is logged, and the
    next run comes as usual."""
    while True:
        await asyncio.sleep(interval)
        try:
            await func()
        except Exception:
            _log.exception("%s failed", getattr(func, "__name__", func))
