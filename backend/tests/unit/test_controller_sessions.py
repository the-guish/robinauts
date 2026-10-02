# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Sessions over the echo engine and the in-memory store."""

from __future__ import annotations

import pytest
from test_controller_turns import opened, settled

from aio import asyncio_test
from robinauts.controller.contract.domain import ActiveTurn, Identity, InvalidValueError, Role


@asyncio_test
async def test_open_session_shows_the_thread_after_the_turn() -> None:
    controller = await opened()
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="hello")
    sid = started.session_id
    await settled(controller, user, started)
    opened_session = await controller.open_session(user, sid)
    assert opened_session.session == await controller._store.get_session(user.id, sid)
    question, answer = opened_session.messages
    assert question == started.question
    assert answer.role is Role.ASSISTANT
    assert answer.parent_id == question.id
    assert opened_session.active is None
    await controller.close()


@asyncio_test
async def test_open_session_shows_the_active_turn() -> None:
    controller = await opened()
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="hello")
    sid = started.session_id
    opened_session = await controller.open_session(user, sid)
    assert opened_session.messages == (started.question,)
    assert isinstance(opened_session.active, ActiveTurn)
    assert opened_session.active.turn_id == started.turn_id
    assert opened_session.active.follows == started.question.id
    await settled(controller, user, started)
    await controller.close()


@asyncio_test
async def test_list_rename_and_delete_sessions() -> None:
    controller = await opened()
    user = await controller.ensure_user(Identity("local", "me"))
    first = await controller.start_session(user, agent="echo", model="echo", text="one")
    await settled(controller, user, first)
    second = await controller.start_session(user, agent="echo", model="echo", text="two")
    await settled(controller, user, second)
    page = await controller.list_sessions(user, limit=10)
    assert [c.id for c in page.sessions] == [second.session_id, first.session_id]

    renamed = await controller.rename_session(user, first.session_id, "First")
    assert renamed.title == "First"
    page = await controller.list_sessions(user, limit=10)
    assert [(c.id, c.title) for c in page.sessions] == [
        (first.session_id, "First"),
        (second.session_id, ""),
    ]

    await controller.delete_session(user, first.session_id)
    page = await controller.list_sessions(user, limit=10)
    assert [c.id for c in page.sessions] == [second.session_id]
    assert not await controller._engines["echo"].exists(first.session_id)
    await controller.close()


@asyncio_test
async def test_sessions_are_listed_a_page_at_a_time_none_twice() -> None:
    controller = await opened()
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
    await controller.close()
