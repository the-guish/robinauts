# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A turn somebody stopped, over a mock engine and the in-process store: what it streamed is
kept as an answer marked stopped, and the next turn's prompt says what it had done."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import create_autospec

from echo_controller import start_echo_controller, wait_for_turn_end

from robinauts.agent_engines.contract.domain import (
    Done,
    ProviderKind,
    TextDelta,
    ToolCall,
    ToolResult,
)
from robinauts.agent_engines.contract.ports import AgentEngine
from robinauts.controller.contract.domain import Identity
from robinauts.shared_domain.domain import TextPart, ToolCallPart, ToolResultPart, TurnState
from util.aio import asyncio_test


@asyncio_test
async def test_a_stopped_turn_keeps_what_it_did_and_the_next_turn_is_told() -> None:
    working = asyncio.Event()

    async def stream(session: uuid.UUID, agent: Any, prompt: str, **_: Any) -> AsyncIterator[Any]:
        if prompt == "look it up":
            yield ToolCall("c1", "search", {"q": "capital"})
            yield ToolResult("c1", "search", "Paris")
            yield TextDelta("The capital")
            working.set()
            # Until somebody stops it.
            await asyncio.Event().wait()
        yield TextDelta("ok")
        yield Done("ok", checkpoint_id="cp")

    engine = create_autospec(AgentEngine, spec_set=True, instance=True)
    engine.kinds.return_value = frozenset(ProviderKind)
    engine.stream.side_effect = stream
    lifecycle = await start_echo_controller(engine)
    controller = lifecycle.composed.controller
    user = await controller.ensure_user(Identity("local", "me"), id=uuid.uuid4())

    started = await controller.start_session(user, agent="echo", model="echo", text="look it up")
    await working.wait()
    await controller.cancel_turn(user, started.session_id, started.turn_id)
    await wait_for_turn_end(controller, user, started)

    opened = await controller.open_session(user, started.session_id)
    assert opened.ended_badly is not None
    assert opened.ended_badly.state is TurnState.CANCELLED
    stopped = opened.messages[-1]
    assert (stopped.stopped, stopped.failed, stopped.checkpoint_id) == (True, False, None)
    assert stopped.turn_id == started.turn_id
    assert stopped.parts == (
        ToolCallPart("c1", "search", {"q": "capital"}),
        ToolResultPart("c1", "Paris"),
        TextPart("The capital"),
    )

    # A reply under the stopped answer: the engine remembers nothing of the stopped turn, so
    # it continues from no checkpoint, and the prompt carries what that turn had done.
    replied = await controller.send_message(
        user, started.session_id, parent_id=stopped.id, model="echo", text="and of Spain?"
    )
    await wait_for_turn_end(controller, user, replied)
    asked = engine.stream.call_args
    assert asked.kwargs["checkpoint_id"] is None
    assert asked.args[2].split("\n") == [
        "Earlier messages in this conversation got no finished answer:",
        "",
        "Message: look it up",
        "The user stopped your answer to it before it finished.",
        "Before it was stopped, it made these tool calls:",
        '- search({"q": "capital"}) -> result, tool output: "Paris"',
        'It had written: "The capital"',
        "",
        "---",
        "The user's new message:",
        "",
        "and of Spain?",
    ]
    thread = (await controller.open_session(user, started.session_id)).messages
    assert [(m.role.value, m.stopped) for m in thread] == [
        ("user", False),
        ("assistant", True),
        ("user", False),
        ("assistant", False),
    ]
    await lifecycle.stop()
