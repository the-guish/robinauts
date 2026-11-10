# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Sessions over the echo engine and the in-process store."""

from __future__ import annotations

import pytest
from echo_controller import start_echo_controller, wait_for_turn_end

from robinauts.controller.contract.domain import Identity, InvalidValueError
from robinauts.shared_domain.domain import DEFAULT_PROJECT, ProjectNotFoundError
from util.aio import asyncio_test


@asyncio_test
async def test_sessions_are_listed_a_page_at_a_time_none_twice() -> None:
    lifecycle = await start_echo_controller()
    controller = lifecycle.composed.controller
    user = await controller.ensure_user(Identity("local", "me"))
    started = []
    for text in ("one", "two", "three"):
        turn = await controller.start_session(user, agent="echo", model="echo", text=text)
        await wait_for_turn_end(controller, user, turn)
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


@asyncio_test
async def test_a_session_is_in_the_default_project_unless_another_is_chosen() -> None:
    lifecycle = await start_echo_controller()
    controller = lifecycle.composed.controller
    user = await controller.ensure_user(Identity("local", "me"))
    other = await controller.ensure_user(Identity("local", "other"))
    (default,) = await controller.list_projects(user)
    assert default.name == DEFAULT_PROJECT
    work = await controller.create_project(user, "  Work  ")
    assert work.name == "Work"
    assert await controller.create_project(user, "Work") == work
    assert await controller.list_projects(user) == (default, work)
    in_default = await controller.start_session(user, agent="echo", model="echo", text="one")
    await wait_for_turn_end(controller, user, in_default)
    in_work = await controller.start_session(
        user, agent="echo", model="echo", text="two", project=work.id
    )
    await wait_for_turn_end(controller, user, in_work)
    page = await controller.list_sessions(user, limit=10)
    assert {s.id: s.project_id for s in page.sessions} == {
        in_default.session_id: default.id,
        in_work.session_id: work.id,
    }
    with pytest.raises(ProjectNotFoundError):
        await controller.start_session(other, agent="echo", model="echo", text="x", project=work.id)
    with pytest.raises(InvalidValueError):
        await controller.create_project(user, " ")
    await lifecycle.stop()
