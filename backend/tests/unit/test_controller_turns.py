# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A turn over the echo engine and the in-memory store."""

from __future__ import annotations

from aio import asyncio_test
from robinauts.controller.contract.domain import (
    AgentConfig,
    Config,
    Identity,
    ModelConfig,
    ProviderConfig,
    ProviderKind,
    Role,
    StorageConfig,
    StorageKind,
    TextPart,
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
    conversation = await controller._store.conversation(started.conversation_id)
    assert conversation is not None
    assert conversation.owner_id == user.id
    assert await controller._store.messages_of(started.conversation_id) == [question]
    await controller._turns[started.conversation_id]
    await controller.close()
