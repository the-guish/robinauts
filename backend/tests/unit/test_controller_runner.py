# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The runner and the dispatcher: the claim, the lease, the parts, cancels, deletes, the close."""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from test_controller_turns import CONFIG, opened, settled

from aio import asyncio_test
from robinauts.agent_engines.contract.domain import Done, Event, TextDelta, ToolCall, ToolResult
from robinauts.agent_engines.echo_engine.engine import EchoEngine
from robinauts.controller.adapters.dispatch import InProcessDispatcher
from robinauts.controller.adapters.memory.store import MemoryStore
from robinauts.controller.application.controller import RobinautsController
from robinauts.controller.contract.domain import (
    AgentConfig,
    Identity,
    NumberedEvent,
    SessionNotFoundError,
    StorageConfig,
    StorageKind,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    TurnActiveError,
    TurnEnded,
    TurnState,
    WorkConfig,
)
from robinauts.controller.core.documents import event_from_document
from robinauts.controller.ports.store import Store

PAST_THE_LEASE = timedelta(hours=1)
"""Past any turn's lease: its deadline, `max_turn_seconds` after its start, and a margin."""


class ScriptedEngine(EchoEngine):
    """Writes text in two pieces, calls its tool, and writes again."""

    async def stream(
        self, session_id: uuid.UUID, agent: Any, prompt: str, **kwargs: Any
    ) -> AsyncGenerator[Event, None]:
        yield TextDelta("Let me ")
        yield TextDelta("look. ")
        yield ToolCall(call_id="c1", name="echo", arguments={"text": prompt})
        yield ToolResult(call_id="c1", name="echo", output=prompt)
        yield TextDelta("Done.")
        yield Done(text="Let me look. Done.", checkpoint_id=str(uuid.uuid4()))


class GatedEngine(EchoEngine):
    """Streams nothing until it is let go, and counts the turns it was asked for."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.streams = 0

    async def stream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Event, None]:
        self.streams += 1
        await self.gate.wait()
        async for event in super().stream(*args, **kwargs):
            yield event


class SlowFinishStore(MemoryStore):
    async def finish_turn(self, *args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(0.1)
        await super().finish_turn(*args, **kwargs)


async def over(store: Store, **options: Any) -> RobinautsController:
    """A controller over that store, wired as the composition wires one."""
    dispatcher = InProcessDispatcher()
    config = options.pop("config", CONFIG)
    controller = RobinautsController(
        config,
        store=store,
        storage=StorageConfig(StorageKind.IN_MEMORY),
        secret_for={}.get,
        dispatcher=dispatcher,
        **options,
    )
    dispatcher.run = controller.run_turn
    await controller.open()
    return controller


def jumped(by: timedelta) -> Any:
    return lambda: datetime.now(UTC) + by


@asyncio_test
async def test_an_answer_keeps_its_parts_in_stream_order() -> None:
    controller = await opened()
    controller._engines["echo"] = ScriptedEngine()
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="hello")
    await settled(controller, user, started)
    answer = (await controller.open_session(user, started.session_id)).messages[-1]
    assert answer.parts == (
        TextPart("Let me look. "),
        ToolCallPart("c1", "echo", {"text": "hello"}),
        ToolResultPart("c1", "hello", False),
        TextPart("Done."),
    )
    assert answer.turn_id == started.turn_id
    await controller.close()


class TimedEngine(EchoEngine):
    """Records the deadline it is handed."""

    def __init__(self) -> None:
        super().__init__()
        self.timeouts: list[float] = []

    async def stream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Event, None]:
        self.timeouts.append(kwargs["timeout_seconds"])
        async for event in super().stream(*args, **kwargs):
            yield event


@asyncio_test
async def test_the_turns_deadline_is_its_own_and_not_the_models_timeout() -> None:
    model = dataclasses.replace(CONFIG.models["echo"], timeout_seconds=30.0)
    config = dataclasses.replace(
        CONFIG, models={"echo": model}, work=WorkConfig(max_turn_seconds=3600.0)
    )
    controller = await over(MemoryStore(), config=config)
    engine = TimedEngine()
    controller._engines["echo"] = engine
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="one")
    await settled(controller, user, started)
    turn = await controller._store.get_turn(user.id, started.session_id, started.turn_id)
    assert turn is not None
    assert turn.deadline_at == turn.started_at + timedelta(hours=1)
    assert turn.lease_until > turn.deadline_at
    [timeout] = engine.timeouts
    assert 3500 < timeout <= 3600
    await controller.close()


@asyncio_test
async def test_two_turns_of_one_session_number_their_events_from_one_each() -> None:
    controller = await opened()
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="one")
    await settled(controller, user, started)
    again = await controller.regenerate_answer(
        user, started.session_id, question_id=started.question.id, model="echo"
    )
    await settled(controller, user, again)
    for turn in (started.turn_id, again.turn_id):
        stored = await controller._store.events_after(user.id, started.session_id, turn, 0)
        assert [p for p, _ in stored] == list(range(1, 10))
    await controller.close()


@asyncio_test
async def test_a_runner_whose_claim_is_refused_runs_no_engine_and_never_finishes() -> None:
    controller = await opened()
    engine = GatedEngine()
    controller._engines["echo"] = engine
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="one")
    sid = started.session_id
    await controller._store.wait_for_events(user.id, sid, started.turn_id, 0, 5.0)
    # A second runner for the same turn, as a duplicate dispatch would be.
    await controller.run_turn(user.id, sid, started.turn_id)
    assert engine.streams == 1
    turn = await controller._store.get_turn(user.id, sid, started.turn_id)
    assert turn is not None
    assert turn.state is TurnState.RUNNING
    engine.gate.set()
    await settled(controller, user, started)
    assert len(await controller._store.messages_of(user.id, sid)) == 2
    await controller.close()


@asyncio_test
async def test_a_runner_refused_mid_stream_writes_nothing_more() -> None:
    controller = await opened()
    engine = GatedEngine()
    controller._engines["echo"] = engine
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="one")
    sid = started.session_id
    await controller._store.wait_for_events(user.id, sid, started.turn_id, 0, 5.0)
    task = controller._dispatcher._tasks[started.turn_id]
    controller._now = jumped(PAST_THE_LEASE)
    assert (await controller.open_session(user, sid)).active is None
    engine.gate.set()
    await task
    turn = await controller._store.get_turn(user.id, sid, started.turn_id)
    assert turn is not None
    assert turn.state is TurnState.INTERRUPTED
    assert len(await controller._store.events_after(user.id, sid, started.turn_id, 0)) == 1
    assert len(await controller._store.messages_of(user.id, sid)) == 1
    await controller.close()


@asyncio_test
async def test_a_turn_whose_lease_has_passed_is_interrupted_and_a_new_turn_starts() -> None:
    controller = await opened()
    engine = GatedEngine()
    controller._engines["echo"] = engine
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="one")
    sid = started.session_id
    await controller._store.wait_for_events(user.id, sid, started.turn_id, 0, 5.0)
    controller._now = jumped(PAST_THE_LEASE)
    watched = [e async for e in controller.watch_turn(user, sid, started.turn_id)]
    assert [type(n.event).__name__ for n in watched] == ["MessageStarted", "TurnEnded"]
    assert watched[-1].event == TurnEnded(TurnState.INTERRUPTED)
    assert watched[-1].position == 1
    engine.gate.set()
    again = await controller.regenerate_answer(
        user, sid, question_id=started.question.id, model="echo"
    )
    await settled(controller, user, again)
    turn = await controller._store.get_turn(user.id, sid, again.turn_id)
    assert turn is not None
    assert turn.state is TurnState.FINISHED
    await controller.close()


@asyncio_test
async def test_a_cancel_before_the_runner_claimed_ends_the_turn_cancelled() -> None:
    controller = await opened()
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="one")
    await controller.cancel_turn(user, started.session_id, started.turn_id)
    # Ended by the controller, not the runner: no event of its own; the record says how.
    assert (
        await controller._store.events_after(user.id, started.session_id, started.turn_id, 0) == []
    )
    watched = [e async for e in controller.watch_turn(user, started.session_id, started.turn_id)]
    assert watched == [NumberedEvent(0, TurnEnded(TurnState.CANCELLED))]
    await controller.close()


@asyncio_test
async def test_a_cancel_of_a_turn_another_process_runs_is_refused() -> None:
    store = MemoryStore()
    first = await over(store)
    engine = GatedEngine()
    first._engines["echo"] = engine
    second = await over(store)
    user = await first.ensure_user(Identity("local", "me"))
    started = await first.start_session(user, agent="echo", model="echo", text="one")
    await store.wait_for_events(user.id, started.session_id, started.turn_id, 0, 5.0)
    with pytest.raises(TurnActiveError):
        await second.cancel_turn(user, started.session_id, started.turn_id)
    engine.gate.set()
    await settled(second, user, started)
    await first.close()
    await second.close()


@asyncio_test
async def test_close_interrupts_a_running_turn_and_keeps_an_answer_being_finished() -> None:
    controller = await opened()
    controller._close_timeout = 0.05
    engine = GatedEngine()
    controller._engines["echo"] = engine
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="one")
    sid = started.session_id
    await controller._store.wait_for_events(user.id, sid, started.turn_id, 0, 5.0)
    await controller.close()
    turn = await controller._store.get_turn(user.id, sid, started.turn_id)
    assert turn is not None
    assert turn.state is TurnState.INTERRUPTED
    stored = await controller._store.events_after(user.id, sid, started.turn_id, 0)
    assert event_from_document(stored[-1][1]).event == TurnEnded(TurnState.INTERRUPTED)

    slow = SlowFinishStore()
    controller = await over(slow, close_timeout=0.01)
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="one")
    await asyncio.sleep(0.02)
    await controller.close()
    turn = await slow.get_turn(user.id, started.session_id, started.turn_id)
    assert turn is not None
    assert turn.state is TurnState.FINISHED
    assert len(await slow.messages_of(user.id, started.session_id)) == 2


@asyncio_test
async def test_delete_during_a_turn_ends_it_cancelled_before_forgetting() -> None:
    controller = await opened()
    engine = GatedEngine()
    controller._engines["echo"] = engine
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="one")
    sid = started.session_id
    await controller._store.wait_for_events(user.id, sid, started.turn_id, 0, 5.0)
    await controller.delete_session(user, sid)
    assert not await engine.exists(sid)
    with pytest.raises(SessionNotFoundError):
        await controller.open_session(user, sid)
    await controller.close()


@asyncio_test
async def test_a_session_whose_engine_the_configuration_no_longer_names_is_deleted() -> None:
    controller = await opened()
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="one")
    await settled(controller, user, started)
    moved = dataclasses.replace(CONFIG.agents["echo"], engine="pydantic-ai")
    controller._config = dataclasses.replace(CONFIG, agents={"echo": moved})
    built_at_open = controller._engines.pop("echo")
    await controller.delete_session(user, started.session_id)
    assert controller._engines["echo"] is not built_at_open
    with pytest.raises(SessionNotFoundError):
        await controller.open_session(user, started.session_id)
    await controller.close()


def test_the_config_names_the_engine_an_agent_runs_on() -> None:
    assert CONFIG.agents["echo"] == AgentConfig(
        "echo", title="Echo", system_prompt="", model="echo", engine="echo"
    )
