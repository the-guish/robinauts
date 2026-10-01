# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Sending, regenerating and cancelling over the echo engine and the in-memory store."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from test_controller_turns import opened

from aio import asyncio_test
from robinauts.agent_engines.contract.domain import Event
from robinauts.agent_engines.echo_engine.engine import EchoEngine
from robinauts.controller.contract.domain import (
    Identity,
    MessageStarted,
    Role,
    TextPart,
    TurnEnded,
    TurnState,
)


class SlowEngine(EchoEngine):
    async def stream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Event, None]:
        await asyncio.sleep(10)
        async for event in super().stream(*args, **kwargs):
            yield event


@asyncio_test
async def test_send_message_runs_a_turn_under_the_answer() -> None:
    controller = await opened()
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_conversation(user, agent="echo", model="echo", text="one")
    cid = started.conversation_id
    await controller._turns[cid]
    first_answer = (await controller.open_conversation(user, cid)).messages[-1]
    sent = await controller.send_message(
        user, cid, parent_id=first_answer.id, model="echo", text="two"
    )
    assert sent.question.parent_id == first_answer.id
    await controller._turns[cid]
    thread = (await controller.open_conversation(user, cid)).messages
    assert len(thread) == 4
    assert thread[:3] == (started.question, first_answer, sent.question)
    assert thread[3].role is Role.ASSISTANT
    assert thread[3].parent_id == sent.question.id
    assert thread[3].parts[-1] == TextPart("The tool said: two")
    await controller.close()


@asyncio_test
async def test_regenerate_answer_runs_a_new_answer_under_the_question() -> None:
    controller = await opened()
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_conversation(user, agent="echo", model="echo", text="one")
    cid = started.conversation_id
    question = started.question
    await controller._turns[cid]
    first_answer = (await controller.open_conversation(user, cid)).messages[-1]
    regenerated = await controller.regenerate_answer(
        user, cid, question_id=question.id, model="echo"
    )
    assert regenerated.question == question
    await controller._turns[cid]
    first_event = (await controller._store.events_after(cid, 0))[0].event
    assert isinstance(first_event, MessageStarted)
    assert first_event.parent_id == question.id
    thread = (await controller.open_conversation(user, cid)).messages
    assert thread[0] == question
    assert thread[1].id == first_event.message_id
    assert thread[1].id != first_answer.id
    assert thread[1].parent_id == question.id
    await controller.close()


@asyncio_test
async def test_cancel_turn_ends_the_turn_cancelled() -> None:
    controller = await opened()
    controller._engines["echo"] = SlowEngine()
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_conversation(user, agent="echo", model="echo", text="one")
    cid = started.conversation_id
    await controller._store.wait_for_events(cid, 0)
    await controller.cancel_turn(user, cid)
    with pytest.raises(asyncio.CancelledError):
        await controller._turns[cid]
    events = await controller._store.events_after(cid, 0)
    assert events[-1].event == TurnEnded(TurnState.CANCELLED)
    assert await controller._store.active_turn(cid) is None
    await controller.close()
