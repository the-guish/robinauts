# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Sessions over the echo engine and the in-memory store."""

from __future__ import annotations

import pytest
from echo_controller import opened, settled

from robinauts.controller.contract.domain import Identity, InvalidValueError
from util.aio import asyncio_test


@asyncio_test
async def test_sessions_are_listed_a_page_at_a_time_none_twice() -> None:
    lifecycle = await opened()
    controller = lifecycle.composed.controller
    user = await controller.ensure_user(Identity("local", "me"))
    started = []
    for text in ("one", "two", "three"):
        turn = await controller.start_session(user, agent="echo", model="echo", text=text)
        await settled(controller, user, turn)
        started.append(turn.session_id)
    first = await controller.list_sessions(user, limit=2)
    assert [s.id for s in first.sessions] == started[:0:-1]
    assert first.cursor is not None
    second = await controller.list_sessions(user, limit=2, cursor=first.cursor)
    assert [s.id for s in second.sessions] == [started[0]]
    assert second.cursor is None
    with pytest.raises(InvalidValueError):
        await controller.list_sessions(user, limit=2, cursor="not a cursor")
    await lifecycle.stop()
