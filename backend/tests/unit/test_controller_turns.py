# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""A turn over the echo engine and the in-memory store."""

from __future__ import annotations

import dataclasses

import pytest

from aio import asyncio_test
from robinauts.controller.composition import build
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
    TurnStarted,
    TurnState,
    UnknownAgentError,
    User,
)
from robinauts.controller.contract.ports import Controller
from robinauts.controller.core.documents import event_from_document, message_from_document

CONFIG = Config(
    providers={"echo": ProviderConfig("echo", ProviderKind.ANTHROPIC, "ECHO_API_KEY")},
    models={"echo": ModelConfig("echo", provider="echo", name="echo", title="Echo")},
    agents={
        "echo": AgentConfig("echo", title="Echo", system_prompt="", model="echo", engine="echo")
    },
)


async def opened() -> Controller:
    controller = build(CONFIG, storage=StorageConfig(StorageKind.IN_MEMORY), secret_for={}.get)
    await controller.open()
    return controller


async def settled(controller: Controller, user: User, started: TurnStarted) -> None:
    """Wait for the turn to end, as a watcher would."""
    async for _ in controller.watch_turn(user, started.session_id, started.turn_id):
        pass


@asyncio_test
async def test_start_session_stores_the_session_and_the_question() -> None:
    controller = await opened()
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="hello")
    question = started.question
    assert question.parts == (TextPart("hello"),)
    assert question.role is Role.USER
    assert question.parent_id is None
    session = await controller._store.get_session(user.id, started.session_id)
    assert session.owner_id == user.id
    assert session.engine == "echo"
    stored = await controller._store.messages_of(user.id, started.session_id)
    assert [message_from_document(d) for d in stored] == [question]
    await settled(controller, user, started)
    await controller.close()


@asyncio_test
async def test_the_turn_stores_its_events_and_the_answer() -> None:
    controller = await opened()
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="hello")
    sid = started.session_id
    await settled(controller, user, started)
    turn = await controller._store.get_turn(user.id, sid, started.turn_id)
    assert turn is not None
    assert turn.state is TurnState.FINISHED
    stored = await controller._store.events_after(user.id, sid, turn.id, 0)
    numbered = [event_from_document(d) for _, d in stored]
    assert [n.position for n in numbered] == list(range(1, 9))
    events = [n.event for n in numbered]
    # The echo streams its answer in two pieces, written together as one.
    assert [type(e) for e in events] == [
        MessageStarted,
        CallStarted,
        ArgumentsPiece,
        CallCompleted,
        ResultLanded,
        TextPiece,
        MessageCompleted,
        TurnEnded,
    ]
    assert events[5] == TextPiece(events[0].message_id, "The tool said: hello")
    assert events[-1] == TurnEnded(TurnState.FINISHED)
    completed = events[-2]
    assert isinstance(completed, MessageCompleted)
    stored = await controller._store.messages_of(user.id, sid)
    question, answer = [message_from_document(d) for d in stored]
    assert question == started.question
    assert answer.id == completed.message_id
    assert answer.parts[-1] == TextPart("The tool said: hello")
    assert answer.parent_id == started.question.id
    assert answer.checkpoint_id is not None
    assert answer.engine == "echo"
    assert answer.turn_id == turn.id
    assert await controller._store.active_turn(user.id, sid) is None
    await controller.close()


@asyncio_test
async def test_watch_turn_follows_the_turn_to_its_end() -> None:
    controller = await opened()
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="hello")
    sid = started.session_id
    watched = [e async for e in controller.watch_turn(user, sid, started.turn_id)]
    assert [n.position for n in watched] == list(range(1, len(watched) + 1))
    assert watched[-1].event == TurnEnded(TurnState.FINISHED)
    again = [
        e async for e in controller.watch_turn(user, sid, started.turn_id, after=len(watched) - 1)
    ]
    assert again == [watched[-1]]
    await controller.close()


@asyncio_test
async def test_a_session_whose_agent_this_configuration_lacks_starts_no_turn() -> None:
    controller = await opened()
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="hello")
    await settled(controller, user, started)
    controller._config = dataclasses.replace(controller._config, agents={})
    shown = await controller.open_session(user, started.session_id)
    with pytest.raises(UnknownAgentError):
        await controller.send_message(
            user, started.session_id, parent_id=shown.messages[-1].id, model="echo", text="again"
        )
    await controller.close()
