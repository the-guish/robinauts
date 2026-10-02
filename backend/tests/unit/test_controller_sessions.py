# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Sessions over the echo engine and the in-memory store."""

from __future__ import annotations

from test_controller_turns import opened

from aio import asyncio_test
from robinauts.controller.contract.domain import ActiveTurn, Identity, Role


@asyncio_test
async def test_open_session_shows_the_thread_after_the_turn() -> None:
    controller = await opened()
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="hello")
    sid = started.session_id
    await controller._turns[sid]
    opened_session = await controller.open_session(user, sid)
    assert opened_session.session == await controller._store.get_session(sid)
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
    await controller._turns[sid]
    await controller.close()


@asyncio_test
async def test_list_rename_and_delete_sessions() -> None:
    controller = await opened()
    user = await controller.ensure_user(Identity("local", "me"))
    first = await controller.start_session(user, agent="echo", model="echo", text="one")
    await controller._turns[first.session_id]
    second = await controller.start_session(user, agent="echo", model="echo", text="two")
    await controller._turns[second.session_id]
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
