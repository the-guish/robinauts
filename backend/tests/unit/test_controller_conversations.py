# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Conversations over the echo engine and the in-memory store."""

from __future__ import annotations

from test_controller_turns import opened

from aio import asyncio_test
from robinauts.controller.contract.domain import ActiveTurn, Identity, Role


@asyncio_test
async def test_open_conversation_shows_the_thread_after_the_turn() -> None:
    controller = await opened()
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_conversation(user, agent="echo", model="echo", text="hello")
    cid = started.conversation_id
    await controller._turns[cid]
    opened_conversation = await controller.open_conversation(user, cid)
    assert opened_conversation.conversation == await controller._store.conversation(cid)
    question, answer = opened_conversation.messages
    assert question == started.question
    assert answer.role is Role.ASSISTANT
    assert answer.parent_id == question.id
    assert opened_conversation.active is None
    await controller.close()


@asyncio_test
async def test_open_conversation_shows_the_active_turn() -> None:
    controller = await opened()
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_conversation(user, agent="echo", model="echo", text="hello")
    cid = started.conversation_id
    opened_conversation = await controller.open_conversation(user, cid)
    assert opened_conversation.messages == (started.question,)
    assert isinstance(opened_conversation.active, ActiveTurn)
    assert opened_conversation.active.follows == started.question.id
    await controller._turns[cid]
    await controller.close()
