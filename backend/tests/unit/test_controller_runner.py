# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The runner and the dispatcher: the claim, the lease, the parts, cancels, deletes, the close."""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from test_controller_turns import CONFIG, opened, settled

from aio import asyncio_test
from robinauts.agent_engines.contract.domain import Done, Event, TextDelta, ToolCall, ToolResult
from robinauts.agent_engines.echo_engine.engine import EchoEngine
from robinauts.controller.adapters.dispatch import InProcessDispatcher
from robinauts.controller.adapters.memory.store import MemoryStore
from robinauts.controller.application import controller as controller_module
from robinauts.controller.application.controller import RobinautsController
from robinauts.controller.contract.domain import (
    AgentConfig,
    Identity,
    NumberedEvent,
    Role,
    SessionNotFoundError,
    StorageConfig,
    StorageKind,
    TextPart,
    TextPiece,
    ToolCallPart,
    ToolResultPart,
    TurnEnded,
    TurnState,
    WorkConfig,
)
from robinauts.controller.core.documents import event_from_document, message_from_document
from robinauts.controller.ports.dispatcher import LOST
from robinauts.controller.ports.store import Store

PAST_THE_LEASE = timedelta(minutes=5)
"""Past the lease of a turn started now, which the heartbeat has not renewed."""


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
    controller = RobinautsController(
        CONFIG,
        store=store,
        storage=StorageConfig(StorageKind.IN_MEMORY),
        secret_for={}.get,
        dispatcher=dispatcher,
        **options,
    )
    dispatcher.run = controller.run_turn
    await controller.open()
    return controller


@contextmanager
def patched_wait(seconds: float) -> Iterator[None]:
    """A cancel's wait for another pod, shortened."""
    kept = controller_module.STOP_WAIT
    controller_module.STOP_WAIT = seconds
    try:
        yield
    finally:
        controller_module.STOP_WAIT = kept


def jumped(by: timedelta) -> Any:
    return lambda: datetime.now(UTC) + by


async def released(pod: Any) -> None:
    """Until the pod's dispatcher holds nothing."""
    while pod._dispatcher.held():
        await asyncio.sleep(0.01)


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
        # The echo's pieces of text, streamed in a row, are written as one event.
        assert [p for p, _ in stored] == list(range(1, 9))
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
    await controller.run_turn(user.id, sid, started.turn_id, 1)
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
    # The reader that ended it kept the answer it had begun, empty and marked failed, and the
    # runner added nothing to it.
    question, left = await controller._messages(user.id, sid)
    assert (left.parent_id, left.failed, left.parts) == (question.id, True, ())
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
async def test_a_cancel_asked_of_another_pod_stops_the_turn_where_it_runs() -> None:
    store = MemoryStore()
    first = await over(store, worker_id="pod-a")
    first._engines["echo"] = GatedEngine()
    second = await over(store, worker_id="pod-b")
    user = await first.ensure_user(Identity("local", "me"))
    started = await first.start_session(user, agent="echo", model="echo", text="one")
    await store.wait_for_events(user.id, started.session_id, started.turn_id, 0, 5.0)
    assert await second.cancel_turn(user, started.session_id, started.turn_id) is True
    turn = await store.get_turn(user.id, started.session_id, started.turn_id)
    assert turn is not None
    assert (turn.state, turn.worker_id) == (TurnState.CANCELLED, "pod-a")
    # Its runner wrote the end; the task is gone a moment later.
    await asyncio.wait_for(released(first), 5.0)
    await first.close()
    await second.close()


@asyncio_test
async def test_a_cancel_no_pod_acts_on_is_recorded_and_ends_the_turn_with_its_lease() -> None:
    store = MemoryStore()
    first = await over(store, worker_id="pod-a")
    first._engines["echo"] = GatedEngine()
    second = await over(store, worker_id="pod-b")
    user = await first.ensure_user(Identity("local", "me"))
    started = await first.start_session(user, agent="echo", model="echo", text="one")
    sid = started.session_id
    await store.wait_for_events(user.id, sid, started.turn_id, 0, 5.0)
    # The pod that runs it is gone: it hears nothing and renews nothing.
    await first._work.stop()
    first._dispatcher._tasks[started.turn_id].cancel(LOST)
    with patched_wait(0.05):
        assert await second.cancel_turn(user, sid, started.turn_id) is False
    second._now = jumped(PAST_THE_LEASE)
    opened_session = await second.open_session(user, sid)
    assert opened_session.ended_badly is not None
    # Asked to stop, so it ends as stopped, and keeps no answer.
    assert opened_session.ended_badly.state is TurnState.CANCELLED
    assert [m.role for m in opened_session.messages] == [Role.USER]
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


class RecordingEngine(EchoEngine):
    """Echo, keeping the limits each turn was streamed with."""

    def __init__(self) -> None:
        super().__init__()
        self.limits: list[dict[str, Any]] = []

    def stream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Event, None]:
        self.limits.append(
            {key: kwargs[key] for key in ("timeout_seconds", "max_model_calls", "model")}
        )
        return super().stream(*args, **kwargs)


@asyncio_test
async def test_a_turn_runs_to_the_works_deadline_not_to_the_models_call_timeout() -> None:
    work = WorkConfig(max_turn_seconds=3 * 3600, max_model_calls=7)
    store = MemoryStore()
    controller = await over(store)
    controller._config = dataclasses.replace(CONFIG, work=work)
    engine = RecordingEngine()
    controller._engines["echo"] = engine
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="hello")
    await settled(controller, user, started)
    turn = await store.get_turn(user.id, started.session_id, started.turn_id)
    assert turn is not None
    assert turn.deadline_at - turn.started_at == timedelta(hours=3)
    # The lease is the heartbeat's, short, whatever the deadline.
    assert turn.lease_until - turn.started_at == timedelta(seconds=90)
    assert (turn.worker_id, turn.attempt) == ("robinauts", 1)
    (limits,) = engine.limits
    assert limits["max_model_calls"] == 7
    # What is left of three hours, and nothing to do with the model's 120 s per call.
    assert 3 * 3600 - 60 < limits["timeout_seconds"] <= 3 * 3600
    await controller.close()


@asyncio_test
async def test_a_turn_the_heartbeat_finds_lost_is_stopped_and_written_to_no_more() -> None:
    store = MemoryStore()
    controller = await over(store)
    engine = GatedEngine()
    controller._engines["echo"] = engine
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="one")
    sid = started.session_id
    await store.wait_for_events(user.id, sid, started.turn_id, 0, 5.0)
    task = controller._dispatcher._tasks[started.turn_id]
    # Another run of the turn holds it now, as after a re-claim elsewhere.
    taken = store._turns[started.turn_id]
    store._turns[started.turn_id] = dataclasses.replace(taken, worker_id="pod-b", attempt=2)
    beat = await controller._work.beat()
    assert beat.lost == {started.turn_id}
    assert task.done()
    assert controller._dispatcher.held() == []
    engine.gate.set()
    assert [p for p, _ in await store.events_after(user.id, sid, started.turn_id, 0)] == [1]
    still = await store.get_turn(user.id, sid, started.turn_id)
    assert still is not None
    assert (still.state, still.attempt) == (TurnState.RUNNING, 2)
    await controller.close()


@asyncio_test
async def test_the_heartbeat_keeps_a_turn_its_runners_past_its_first_lease() -> None:
    work = WorkConfig(lease_seconds=0.6, heartbeat_seconds=0.1)
    store = MemoryStore()
    controller = RobinautsController(
        dataclasses.replace(CONFIG, work=work),
        store=store,
        storage=StorageConfig(StorageKind.IN_MEMORY),
        secret_for={}.get,
        dispatcher=(dispatcher := InProcessDispatcher()),
    )
    dispatcher.run = controller.run_turn
    await controller.open()
    engine = GatedEngine()
    controller._engines["echo"] = engine
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="one")
    first = await store.get_turn(user.id, started.session_id, started.turn_id)
    await asyncio.sleep(1.2)
    engine.gate.set()
    await settled(controller, user, started)
    ended = await store.get_turn(user.id, started.session_id, started.turn_id)
    assert first is not None
    assert ended is not None
    assert ended.state is TurnState.FINISHED
    assert ended.heartbeat_at is not None
    assert first.heartbeat_at is not None
    assert ended.heartbeat_at > first.heartbeat_at + timedelta(seconds=0.6)
    await controller.close()


class HalfwayEngine(EchoEngine):
    """Says something, calls its tool, and then waits, for ever unless let go."""

    def __init__(self) -> None:
        super().__init__()
        self.halfway = asyncio.Event()
        self.gate = asyncio.Event()

    async def stream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Event, None]:
        yield TextDelta("Let me look. ")
        yield ToolCall(call_id="c1", name="echo", arguments={"text": "x"})
        yield ToolResult(call_id="c1", name="echo", output="x")
        self.halfway.set()
        await self.gate.wait()
        yield Done(text="done", checkpoint_id=str(uuid.uuid4()))


HALFWAY = (
    TextPart("Let me look. "),
    ToolCallPart("c1", "echo", {"text": "x"}),
    ToolResultPart("c1", "x", False),
)


@asyncio_test
async def test_a_turn_whose_pod_died_is_shown_with_what_it_did_and_can_be_retried() -> None:
    store = MemoryStore()
    controller = await over(store)
    engine = HalfwayEngine()
    controller._engines["echo"] = engine
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="one")
    sid = started.session_id
    await engine.halfway.wait()
    # The pod is gone: its heartbeat stops, and its runner with it.
    await controller._work.stop()
    controller._dispatcher._tasks[started.turn_id].cancel(LOST)
    controller._now = jumped(PAST_THE_LEASE)
    opened_session = await controller.open_session(user, sid)
    assert opened_session.active is None
    assert opened_session.ended_badly is not None
    assert opened_session.ended_badly.state is TurnState.INTERRUPTED
    left = opened_session.messages[-1]
    assert (left.failed, left.parts, left.turn_id) == (True, HALFWAY, started.turn_id)
    retried = await controller.retry_answer(user, sid, answer_id=left.id, model="echo")
    engine.gate.set()
    controller._now = lambda: datetime.now(UTC)
    await settled(controller, user, retried)
    turn = await store.get_turn(user.id, sid, retried.turn_id)
    assert turn is not None
    assert (turn.state, turn.retries) == (TurnState.FINISHED, left.id)
    await controller.close()


@asyncio_test
async def test_a_turn_the_deployment_stops_keeps_what_it_did_as_a_failed_answer() -> None:
    store = MemoryStore()
    controller = await over(store, close_timeout=0.05)
    engine = HalfwayEngine()
    controller._engines["echo"] = engine
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="one")
    await engine.halfway.wait()
    await controller.close()
    turn = await store.get_turn(user.id, started.session_id, started.turn_id)
    assert turn is not None
    assert turn.state is TurnState.INTERRUPTED
    documents = await store.messages_of(user.id, started.session_id)
    left = message_from_document(documents[-1])
    assert (left.failed, left.parts) == (True, HALFWAY)


class ChattyEngine(EchoEngine):
    """Streams its answer a token at a time, then pauses until let go before it is done."""

    def __init__(self, tokens: int) -> None:
        super().__init__()
        self.tokens = tokens
        self.gate = asyncio.Event()

    async def stream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Event, None]:
        for n in range(self.tokens):
            yield TextDelta(f"{n} ")
        await self.gate.wait()
        yield Done(text="", checkpoint_id=str(uuid.uuid4()))


class CountingStore(MemoryStore):
    """Counts the writes of events, each of which is one announcement to the watchers."""

    def __init__(self) -> None:
        super().__init__()
        self.batches = 0

    async def append_events(self, *args: Any, **kwargs: Any) -> None:
        self.batches += 1
        await super().append_events(*args, **kwargs)


@asyncio_test
async def test_text_streamed_a_token_at_a_time_is_written_a_batch_at_a_time() -> None:
    store = CountingStore()
    controller = await over(store)
    engine = ChattyEngine(tokens=500)
    controller._engines["echo"] = engine
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="one")
    sid = started.session_id
    # The text that waited is written by the timer, while the engine says nothing more.
    async with asyncio.timeout(2.0):
        while await store.last_position(user.id, sid, started.turn_id) < 2:
            await store.wait_for_events(user.id, sid, started.turn_id, 1, 1.0)
    engine.gate.set()
    await settled(controller, user, started)
    stored = await store.events_after(user.id, sid, started.turn_id, 0)
    events = [event_from_document(d).event for _, d in stored]
    pieces = [e for e in events if isinstance(e, TextPiece)]
    assert "".join(p.text for p in pieces) == "".join(f"{n} " for n in range(500))
    assert len(pieces) < 5
    assert store.batches < 5
    answer = message_from_document((await store.messages_of(user.id, sid))[-1])
    assert answer.parts == (TextPart("".join(f"{n} " for n in range(500))),)
    await controller.close()
