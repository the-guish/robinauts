# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A turn over the echo engine and the in-memory store."""

from __future__ import annotations

from aio import asyncio_test
from robinauts.controller.contract.domain import (
    AgentConfig,
    ArgumentsPiece,
    CallCompleted,
    CallStarted,
    Config,
    Identity,
    MessageCompleted,
    MessageStarted,
    ModelConfig,
    ProviderConfig,
    ProviderKind,
    ResultLanded,
    Role,
    StorageConfig,
    StorageKind,
    TextPart,
    TextPiece,
    TurnEnded,
    TurnState,
)
from robinauts.controller.controller import RobinautsController

CONFIG = Config(
    providers={"echo": ProviderConfig("echo", ProviderKind.ANTHROPIC, "ECHO_API_KEY")},
    models={"echo": ModelConfig("echo", provider="echo", name="echo", title="Echo")},
    agents={
        "echo": AgentConfig("echo", title="Echo", system_prompt="", model="echo", engine="echo")
    },
)


async def opened() -> RobinautsController:
    controller = RobinautsController(
        CONFIG, storage=StorageConfig(StorageKind.IN_MEMORY), secret_for={}.get
    )
    await controller.open()
    return controller


@asyncio_test
async def test_start_conversation_stores_the_conversation_and_the_question() -> None:
    controller = await opened()
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_conversation(user, agent="echo", model="echo", text="hello")
    question = started.question
    assert question.parts == (TextPart("hello"),)
    assert question.role is Role.USER
    assert question.parent_id is None
    conversation = await controller._store.get_conversation(started.conversation_id)
    assert conversation is not None
    assert conversation.owner_id == user.id
    assert await controller._store.messages_of(started.conversation_id) == [question]
    await controller._turns[started.conversation_id]
    await controller.close()


@asyncio_test
async def test_the_turn_stores_its_events_and_the_answer() -> None:
    controller = await opened()
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_conversation(user, agent="echo", model="echo", text="hello")
    cid = started.conversation_id
    await controller._turns[cid]
    numbered = await controller._store.events_after(cid, 0)
    assert [n.position for n in numbered] == list(range(1, 10))
    events = [n.event for n in numbered]
    assert [type(e) for e in events] == [
        MessageStarted,
        CallStarted,
        ArgumentsPiece,
        CallCompleted,
        ResultLanded,
        TextPiece,
        TextPiece,
        MessageCompleted,
        TurnEnded,
    ]
    assert events[-1] == TurnEnded(TurnState.FINISHED)
    completed = events[-2]
    assert isinstance(completed, MessageCompleted)
    answer = completed.message
    assert answer.parts[-1] == TextPart("The tool said: hello")
    assert answer.parent_id == started.question.id
    assert answer.checkpoint_id is not None
    assert await controller._store.messages_of(cid) == [started.question, answer]
    assert await controller._store.active_turn(cid) is None
    await controller.close()


@asyncio_test
async def test_watch_turn_follows_the_turn_to_its_end() -> None:
    controller = await opened()
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_conversation(user, agent="echo", model="echo", text="hello")
    cid = started.conversation_id
    watched = [e async for e in controller.watch_turn(user, cid)]
    assert [n.position for n in watched] == list(range(1, len(watched) + 1))
    assert watched[-1].event == TurnEnded(TurnState.FINISHED)
    again = [e async for e in controller.watch_turn(user, cid, after=len(watched) - 1)]
    assert again == [watched[-1]]
    await controller.close()
