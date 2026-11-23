# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Sessions over the echo engine and the in-process store."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import aclosing
from typing import Any
from unittest.mock import patch

import pytest
from echo_controller import start_echo_controller, wait_for_turn_end

from robinauts.agent_engines.contract.domain import Event, ToolResult
from robinauts.agent_engines.echo_engine.engine import EchoEngine
from robinauts.controller.adapters.in_process.store import InProcessStore
from robinauts.controller.contract.domain import Identity, InvalidValueError
from robinauts.shared_domain.domain import DEFAULT_PROJECT, ProjectNotFoundError, ResultLanded
from util.aio import asyncio_test


@asyncio_test
async def test_sessions_are_listed_a_page_at_a_time_none_twice() -> None:
    lifecycle = await start_echo_controller()
    controller = lifecycle.composed.controller
    user = await controller.ensure_user(Identity("local", "me"), id=uuid.uuid4())
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
async def test_sessions_are_searched_by_title_project_and_agent_a_page_at_a_time() -> None:
    lifecycle = await start_echo_controller()
    controller = lifecycle.composed.controller
    user = await controller.ensure_user(Identity("local", "me"), id=uuid.uuid4())
    work = await controller.create_project(user, "Work")
    ids = {}
    for text, project in (("apple pie", None), ("banana", work.id), ("apple tart", work.id)):
        turn = await controller.start_session(
            user, agent="echo", model="echo", text=text, project=project
        )
        await wait_for_turn_end(controller, user, turn)
        ids[text] = turn.session_id
    first = await controller.list_sessions(user, limit=1, query="APPLE")
    assert [s.id for s in first.sessions] == [ids["apple tart"]]
    assert first.cursor is not None
    second = await controller.list_sessions(user, limit=1, query="apple", cursor=first.cursor)
    assert [s.id for s in second.sessions] == [ids["apple pie"]]
    # The last match ends the search: no cursor to an empty page.
    assert second.cursor is None
    exact = await controller.list_sessions(user, limit=2, query="apple")
    assert len(exact.sessions) == 2
    assert exact.cursor is None
    blanks = await controller.list_sessions(user, limit=10, query="  \t ")
    assert len(blanks.sessions) == 3
    by_project = await controller.list_sessions(user, limit=10, query="work apple")
    assert [s.id for s in by_project.sessions] == [ids["apple tart"]]
    assert by_project.cursor is None
    assert (await controller.list_sessions(user, limit=10, query="echo")).sessions
    assert not (await controller.list_sessions(user, limit=10, query="zzz")).sessions
    await lifecycle.stop()


@asyncio_test
async def test_a_session_is_in_the_default_project_unless_another_is_chosen() -> None:
    lifecycle = await start_echo_controller()
    controller = lifecycle.composed.controller
    user = await controller.ensure_user(Identity("local", "me"), id=uuid.uuid4())
    other = await controller.ensure_user(Identity("local", "other"), id=uuid.uuid4())
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


@asyncio_test
async def test_a_call_s_result_is_read_though_its_turn_ends_between_the_two_reads() -> None:
    """A result is among its turn's events while the turn runs, and in the stored answer once
    it has ended. Here the turn ends after the first of the controller's two reads and before
    the second, as it can on a store that is asked over a network."""
    release = asyncio.Event()

    class HeldAfterItsTool(EchoEngine):
        async def stream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Event, None]:
            async for event in super().stream(*args, **kwargs):
                yield event
                if isinstance(event, ToolResult):
                    await release.wait()

    lifecycle = await start_echo_controller(HeldAfterItsTool())
    controller = lifecycle.composed.controller
    user = await controller.ensure_user(Identity("local", "me"), id=uuid.uuid4())
    started = await controller.start_session(user, agent="echo", model="echo", text="one")
    landed = None
    async with aclosing(controller.watch_turn(user, started.session_id, started.turn_id)) as seen:
        async for numbered in seen:
            if isinstance(numbered.event, ResultLanded):
                landed = numbered.event
                break
    assert landed is not None

    running = InProcessStore.active_turn
    ended = False

    def then_the_turn_ends(read: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        async def reading(store: InProcessStore, owner: uuid.UUID, session: uuid.UUID) -> Any:
            nonlocal ended
            found = await read(store, owner, session)
            if not ended:
                ended = True
                release.set()
                async with asyncio.timeout(10):
                    while await running(store, owner, session) is not None:
                        await asyncio.sleep(0.01)
            return found

        return reading

    with (
        patch.object(InProcessStore, "active_turn", then_the_turn_ends(running)),
        patch.object(InProcessStore, "messages_of", then_the_turn_ends(InProcessStore.messages_of)),
    ):
        result = await controller.tool_result(
            user, started.session_id, message_id=landed.message_id, call_id=landed.call_id
        )
    assert ended
    assert (result.text, result.structured) == ("one", {"text": "one"})
    await lifecycle.stop()
