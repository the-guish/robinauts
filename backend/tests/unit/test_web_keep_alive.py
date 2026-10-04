# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A turn's stream says something at least every so often, and loses nothing for it."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from aio import asyncio_test
from robinauts.web.agui import KEEP_ALIVE, kept_alive


async def slow(*chunks: tuple[float, str]) -> AsyncIterator[str]:
    for delay, chunk in chunks:
        await asyncio.sleep(delay)
        yield chunk


@asyncio_test
async def test_a_quiet_stream_is_kept_alive_and_its_chunks_arrive_in_order() -> None:
    said = [c async for c in kept_alive(slow((0.0, "a"), (0.35, "b"), (0.0, "c")), 0.1)]
    assert [c for c in said if c != KEEP_ALIVE] == ["a", "b", "c"]
    assert 2 <= said.count(KEEP_ALIVE) <= 4
    assert said.index("b") > said.index(KEEP_ALIVE)
    assert KEEP_ALIVE == ": keep-alive\n\n"


@asyncio_test
async def test_a_busy_stream_needs_no_keep_alive() -> None:
    said = [c async for c in kept_alive(slow((0.0, "a"), (0.0, "b")), 1.0)]
    assert said == ["a", "b"]


@asyncio_test
async def test_closing_the_stream_early_closes_what_it_waits_on() -> None:
    closed = asyncio.Event()

    async def endless() -> AsyncIterator[str]:
        try:
            yield "a"
            await asyncio.sleep(3600)
            yield "never"
        finally:
            closed.set()

    stream = kept_alive(endless(), 0.05)
    assert await anext(stream) == "a"
    assert await anext(stream) == KEEP_ALIVE
    await stream.aclose()
    assert closed.is_set()
