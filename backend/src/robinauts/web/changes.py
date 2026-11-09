# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The wait behind ``GET /api/changes`` (``docs/specs/wire.md``, "The change feed").

A poll holds its request open for up to half a minute. A tab that closes, and a server that
stops, end the wait at once. Otherwise every stop would wait for the slowest open tab, and a
closed tab would hold a task until its timeout.
"""

from __future__ import annotations

import asyncio

from starlette.requests import Request

from robinauts.controller.contract.domain import User
from robinauts.controller.contract.ports import Controller


class FeedGate:
    """What ends every wait of the feed: the server stopping. Whoever stops the server closes
    it before the server waits for its open requests."""

    def __init__(self) -> None:
        self._closed = asyncio.Event()

    def close(self) -> None:
        self._closed.set()

    async def closed(self) -> None:
        await self._closed.wait()


async def _gone(request: Request) -> None:
    """Return once the client has gone away."""
    while (await request.receive())["type"] != "http.disconnect":
        pass


async def changes(
    controller: Controller,
    user: User,
    after: int | None,
    timeout: float,
    request: Request,
    gate: FeedGate,
) -> int:
    """The user's change version, as ``Controller.wait_for_changes`` answers it, or ``after``
    unchanged once the client goes away or the server stops."""
    if after is None:
        return await controller.wait_for_changes(user, after=None, timeout=0)
    waited = asyncio.ensure_future(controller.wait_for_changes(user, after=after, timeout=timeout))
    ending = [asyncio.ensure_future(_gone(request)), asyncio.ensure_future(gate.closed())]
    try:
        await asyncio.wait({waited, *ending}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for each in (waited, *ending):
            if not each.done():
                each.cancel()
        await asyncio.gather(waited, *ending, return_exceptions=True)
    return after if waited.cancelled() else waited.result()
