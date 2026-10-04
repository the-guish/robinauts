# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The runner and the dispatcher: the claim, the lease, the parts, cancels, deletes, the close."""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import AsyncGenerator, Callable
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
    BusyError,
    DrainingError,
    Identity,
    MessageStarted,
    NumberedEvent,
    Session,
    SessionNotFoundError,
    StorageConfig,
    StorageKind,
    Swept,
    TextPart,
    TextPiece,
    ToolCallPart,
    ToolResultPart,
    TurnActiveError,
    TurnEnded,
    TurnState,
    UnknownModelError,
    WorkConfig,
)
from robinauts.controller.core.documents import event_from_document, message_from_document
from robinauts.controller.ports.dispatcher import StopReason
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
        work=store,
        worker=options.pop("worker", "pod-a"),
        close_timeout=options.pop("close_timeout", 0.1),
        **options,
    )
    dispatcher.run = controller.run_turn
    await controller.open()
    return controller


async def until(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    """Wait for what another task does next, such as a runner leaving its dispatcher."""
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


async def until_async(read: Callable[[], Any], holds: Callable[[Any], bool]) -> None:
    async with asyncio.timeout(5.0):
        while not holds(await read()):
            await asyncio.sleep(0.02)


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
    # The lease is the heartbeat's, apart from the deadline.
    assert turn.lease_until == turn.started_at + timedelta(seconds=90)
    [timeout] = engine.timeouts
    assert 3500 < timeout <= 3600
    await controller.close()


class ChattyEngine(EchoEngine):
    """Streams many small pieces at once, pauses, streams one more, and waits to be let go."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()

    async def stream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Event, None]:
        for word in ["one ", "two ", "three ", "four "]:
            yield TextDelta(word)
        await asyncio.sleep(0.5)
        yield TextDelta("five")
        await self.gate.wait()
        yield Done(text="one two three four five", checkpoint_id=str(uuid.uuid4()))


@asyncio_test
async def test_pieces_of_text_are_written_together_and_soon_whether_or_not_more_follow() -> None:
    store = MemoryStore()
    controller = await over(store)
    engine = ChattyEngine()
    controller._engines["echo"] = engine
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="count")
    sid = started.session_id
    # Nothing follows "five" until the gate opens: it is written all the same, on its own.
    await until_async(lambda: store.last_position(user.id, sid, started.turn_id), lambda n: n >= 3)
    stored = await store.events_after(user.id, sid, started.turn_id, 0)
    pieces = [event_from_document(d).event for _, d in stored]
    assert [type(p).__name__ for p in pieces] == ["MessageStarted", "TextPiece", "TextPiece"]
    assert [p.text for p in pieces[1:]] == ["one two three four ", "five"]  # type: ignore[union-attr]
    opened_now = await controller.open_session(user, sid)
    assert opened_now.active is not None
    assert opened_now.active.position == 3
    engine.gate.set()
    await settled(controller, user, started)
    answer = (await controller.open_session(user, sid)).messages[-1]
    assert answer.parts == (TextPart("one two three four five"),)
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
    _, task = controller._dispatcher._tasks[started.turn_id]
    controller._now = jumped(PAST_THE_LEASE)
    assert (await controller.open_session(user, sid)).active is None
    engine.gate.set()
    await task
    turn = await controller._store.get_turn(user.id, sid, started.turn_id)
    assert turn is not None
    assert turn.state is TurnState.INTERRUPTED
    # The reader that ended it wrote the end, and the answer as far as it had gone; the
    # runner, refused, wrote nothing more.
    assert len(await controller._store.events_after(user.id, sid, started.turn_id, 0)) == 2
    assert len(await controller._store.messages_of(user.id, sid)) == 2
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
    assert watched[-1].position == 2
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
async def test_the_heartbeat_keeps_a_turn_past_the_lease_it_started_with() -> None:
    config = dataclasses.replace(CONFIG, work=WorkConfig(lease_seconds=0.6, heartbeat_seconds=0.1))
    controller = await over(MemoryStore(), config=config)
    engine = GatedEngine()
    controller._engines["echo"] = engine
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="one")
    sid = started.session_id
    await controller._store.wait_for_events(user.id, sid, started.turn_id, 0, 5.0)
    first = await controller._store.get_turn(user.id, sid, started.turn_id)
    await asyncio.sleep(1.2)
    assert first is not None
    assert datetime.now(UTC) > first.lease_until
    renewed = await controller._store.get_turn(user.id, sid, started.turn_id)
    assert renewed is not None
    assert renewed.lease_until > first.lease_until
    assert renewed.heartbeat_at is not None
    assert renewed.heartbeat_at > first.started_at
    assert (await controller.open_session(user, sid)).active is not None
    engine.gate.set()
    await settled(controller, user, started)
    turn = await controller._store.get_turn(user.id, sid, started.turn_id)
    assert turn is not None
    assert turn.state is TurnState.FINISHED
    await controller.close()


@asyncio_test
async def test_a_turn_the_heartbeat_did_not_renew_is_stopped_and_writes_nothing_more() -> None:
    store = MemoryStore()
    controller = await over(store)
    engine = GatedEngine()
    controller._engines["echo"] = engine
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="one")
    sid = started.session_id
    await store.wait_for_events(user.id, sid, started.turn_id, 0, 5.0)
    # Another process holds it now, under another attempt.
    taken = dataclasses.replace(store._turns[started.turn_id], worker_id="pod-b", attempt=2)
    store._turns[started.turn_id] = taken
    await controller._work_loop.beat()
    assert controller._dispatcher.held() == []
    assert await store.get_turn(user.id, sid, started.turn_id) == taken
    assert len(await store.events_after(user.id, sid, started.turn_id, 0)) == 1
    assert len(await store.messages_of(user.id, sid)) == 1
    await controller.close()


class HangingEngine(EchoEngine):
    """Writes, calls its tool and has the result, then never answers; records its prompts."""

    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[str] = []

    async def stream(
        self, session_id: uuid.UUID, agent: Any, prompt: str, **kwargs: Any
    ) -> AsyncGenerator[Event, None]:
        self.prompts.append(prompt)
        yield TextDelta("Let me ")
        yield TextDelta("look. ")
        yield ToolCall(call_id="c1", name="search", arguments={"q": "robins"})
        yield ToolResult(call_id="c1", name="search", output="three hits")
        await asyncio.Event().wait()
        yield Done(text="never", checkpoint_id="never")


@asyncio_test
async def test_an_expired_turn_keeps_its_partial_answer_as_failed_and_retry_starts_over() -> None:
    store = MemoryStore()
    controller = await over(store)
    engine = HangingEngine()
    controller._engines["echo"] = engine
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="find them")
    sid = started.session_id
    while len(await store.events_after(user.id, sid, started.turn_id, 0)) < 6:
        await store.wait_for_events(user.id, sid, started.turn_id, 0, 0.05)
    # Its process went away: nothing renews the lease, and the runner is gone.
    await controller._work_loop.stop()
    _, task = controller._dispatcher._tasks.pop(started.turn_id)
    task.cancel(StopReason.LOST)
    controller._now = jumped(PAST_THE_LEASE)
    opened_after = await controller.open_session(user, sid)
    assert opened_after.active is None
    assert opened_after.ended_badly is not None
    assert opened_after.ended_badly.state is TurnState.INTERRUPTED
    partial = opened_after.messages[-1]
    assert partial.failed
    assert partial.turn_id == started.turn_id
    assert partial.parent_id == started.question.id
    assert partial.parts == (
        TextPart("Let me look. "),
        ToolCallPart("c1", "search", {"q": "robins"}),
        ToolResultPart("c1", "three hits", False),
    )
    # The thread showed it under the id it streamed with.
    stored = await store.events_after(user.id, sid, started.turn_id, 0)
    first = event_from_document(stored[0][1]).event
    assert isinstance(first, MessageStarted)
    assert first.message_id == partial.id
    assert event_from_document(stored[-1][1]).event == TurnEnded(TurnState.INTERRUPTED)
    retried = await controller.retry_answer(user, sid, answer_id=partial.id, model="echo")
    assert retried.question == started.question
    await store.wait_for_events(user.id, sid, retried.turn_id, 0, 5.0)
    assert 'search({"q": "robins"}) -> result, tool output: "three hits"' in engine.prompts[-1]
    assert "pressed the retry button" in engine.prompts[-1]
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
async def test_a_cancel_through_another_process_stops_the_turn_where_it_runs() -> None:
    store = MemoryStore()
    first = await over(store, worker="pod-a")
    engine = GatedEngine()
    first._engines["echo"] = engine
    second = await over(store, worker="pod-b")
    user = await first.ensure_user(Identity("local", "me"))
    started = await first.start_session(user, agent="echo", model="echo", text="one")
    sid = started.session_id
    await store.wait_for_events(user.id, sid, started.turn_id, 0, 5.0)
    assert await second.cancel_turn(user, sid, started.turn_id) is True
    turn = await store.get_turn(user.id, sid, started.turn_id)
    assert turn is not None
    assert turn.state is TurnState.CANCELLED
    assert turn.cancel_requested_at is not None
    await until(lambda: first._dispatcher.held() == [])
    watched = [e async for e in second.watch_turn(user, sid, started.turn_id)]
    assert watched[-1].event == TurnEnded(TurnState.CANCELLED)
    await first.close()
    await second.close()


@asyncio_test
async def test_a_cancel_whose_signal_was_lost_is_acted_on_at_the_next_heartbeat() -> None:
    store = MemoryStore()
    controller = await over(store, cancel_wait=0.05)
    engine = GatedEngine()
    controller._engines["echo"] = engine
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="one")
    sid = started.session_id
    await store.wait_for_events(user.id, sid, started.turn_id, 0, 5.0)
    # Asked for through the store, by a process whose signal never arrived.
    store._cancels.clear()
    asked = await store.request_cancel(user.id, sid, started.turn_id, datetime.now(UTC))
    assert asked is not None
    assert asked.cancel_requested_at is not None
    assert controller._dispatcher.held() != []
    await controller._work_loop.beat()
    await asyncio.wait_for(settled(controller, user, started), 5.0)
    turn = await store.get_turn(user.id, sid, started.turn_id)
    assert turn is not None
    assert turn.state is TurnState.CANCELLED
    await controller.close()


@asyncio_test
async def test_a_cancel_of_a_turn_whose_holder_went_away_ends_it_cancelled_at_its_lease() -> None:
    store = MemoryStore()
    gone = await over(store, worker="pod-a")
    gone._engines["echo"] = HangingEngine()
    here = await over(store, worker="pod-b", cancel_wait=0.05)
    user = await gone.ensure_user(Identity("local", "me"))
    started = await gone.start_session(user, agent="echo", model="echo", text="one")
    sid = started.session_id
    while len(await store.events_after(user.id, sid, started.turn_id, 0)) < 6:
        await store.wait_for_events(user.id, sid, started.turn_id, 0, 0.05)
    # The holder went away, signals and all.
    await gone._work_loop.stop()
    _, task = gone._dispatcher._tasks.pop(started.turn_id)
    task.cancel(StopReason.LOST)
    assert await here.cancel_turn(user, sid, started.turn_id) is False
    here._now = jumped(PAST_THE_LEASE)
    opened_after = await here.open_session(user, sid)
    assert opened_after.active is None
    assert opened_after.ended_badly is not None
    assert opened_after.ended_badly.state is TurnState.CANCELLED
    # Cancelled, as its runner would have ended it: no answer is stored.
    assert [m.id for m in opened_after.messages] == [started.question.id]
    await gone.close()
    await here.close()


@asyncio_test
async def test_a_delete_through_another_process_stops_the_turn_and_then_purges() -> None:
    store = MemoryStore()
    first = await over(store, worker="pod-a")
    engine = GatedEngine()
    first._engines["echo"] = engine
    second = await over(store, worker="pod-b")
    second._engines["echo"] = first._engines["echo"]
    user = await first.ensure_user(Identity("local", "me"))
    started = await first.start_session(user, agent="echo", model="echo", text="one")
    sid = started.session_id
    await store.wait_for_events(user.id, sid, started.turn_id, 0, 5.0)
    await second.delete_session(user, sid)
    await until(lambda: first._dispatcher.held() == [])
    assert not await engine.exists(sid)
    with pytest.raises(SessionNotFoundError):
        await first.open_session(user, sid)
    await first.close()
    await second.close()


@asyncio_test
async def test_a_delete_is_refused_while_a_turn_has_not_stopped_in_time() -> None:
    store = MemoryStore()
    gone = await over(store, worker="pod-a")
    gone._engines["echo"] = GatedEngine()
    here = await over(store, worker="pod-b", delete_wait=0.05)
    user = await gone.ensure_user(Identity("local", "me"))
    started = await gone.start_session(user, agent="echo", model="echo", text="one")
    sid = started.session_id
    await store.wait_for_events(user.id, sid, started.turn_id, 0, 5.0)
    await gone._work_loop.stop()
    with pytest.raises(TurnActiveError):
        await here.delete_session(user, sid)
    assert (await here.open_session(user, sid)).active is not None
    await gone.close()
    await here.close()


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
    # What it had streamed, nothing here, is kept as an answer that failed.
    question, interrupted = await controller._store.messages_of(user.id, sid)
    assert message_from_document(interrupted).failed

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


class StuckFinishStore(MemoryStore):
    """A database that stopped answering: no turn's end ever lands."""

    async def finish_turn(self, *args: Any, **kwargs: Any) -> None:
        await asyncio.Event().wait()


@asyncio_test
async def test_close_waits_a_bounded_time_for_turns_that_cannot_write_their_end() -> None:
    stuck = StuckFinishStore()
    controller = await over(stuck, close_timeout=0.05, final_wait=0.2)
    controller._engines["echo"] = GatedEngine()
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="one")
    await stuck.wait_for_events(user.id, started.session_id, started.turn_id, 0, 5.0)
    await asyncio.wait_for(controller.close(), 2.0)
    turn = await stuck.get_turn(user.id, started.session_id, started.turn_id)
    assert turn is not None
    # Left to its lease, which whoever finds it next ends.
    assert turn.state is TurnState.RUNNING


@asyncio_test
async def test_a_draining_controller_is_not_ready_and_takes_no_new_turn() -> None:
    controller = await over(MemoryStore())
    user = await controller.ensure_user(Identity("local", "me"))
    assert (await controller.readiness()).ready
    await controller.drain()
    readiness = await controller.readiness()
    assert not readiness.ready
    assert readiness.problems == ("this process is stopping",)
    with pytest.raises(DrainingError):
        await controller.start_session(user, agent="echo", model="echo", text="one")
    await controller.close()


class BusyStore(MemoryStore):
    """A database whose pool had no connection free, twice, for the turn's writes."""

    def __init__(self) -> None:
        super().__init__()
        self.refused = 0

    async def append_events(self, *args: Any, **kwargs: Any) -> None:
        if self.refused < 2:
            self.refused += 1
            raise BusyError("no connection was free")
        await super().append_events(*args, **kwargs)


@asyncio_test
async def test_a_write_the_database_had_no_connection_for_is_tried_again() -> None:
    busy = BusyStore()
    controller = await over(busy)
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="one")
    await asyncio.wait_for(settled(controller, user, started), 10.0)
    assert busy.refused == 2
    turn = await busy.get_turn(user.id, started.session_id, started.turn_id)
    assert turn is not None
    assert turn.state is TurnState.FINISHED
    await controller.close()


@asyncio_test
async def test_the_sweep_ends_expired_turns_purges_what_a_delete_left_and_deletes_events() -> None:
    store = MemoryStore()
    gone = await over(store, worker="pod-a")
    gone._engines["echo"] = HangingEngine()
    here = await over(store, worker="pod-b")
    user = await gone.ensure_user(Identity("local", "me"))
    started = await gone.start_session(user, agent="echo", model="echo", text="one")
    sid = started.session_id
    while len(await store.events_after(user.id, sid, started.turn_id, 0)) < 6:
        await store.wait_for_events(user.id, sid, started.turn_id, 0, 0.05)
    await gone._work_loop.stop()
    _, task = gone._dispatcher._tasks.pop(started.turn_id)
    task.cancel(StopReason.LOST)
    # A conversation whose delete hid it and died before the purge.
    left = await here.start_session(user, agent="echo", model="echo", text="two")
    await settled(here, user, left)
    await store.hide_session(user.id, left.session_id, datetime.now(UTC))
    assert await here.sweep() == Swept()
    here._now = jumped(PAST_THE_LEASE)
    swept = await here.sweep()
    assert (swept.turns_ended, swept.sessions_purged) == (1, 1)
    ended = await store.get_turn(user.id, sid, started.turn_id)
    assert ended is not None
    assert ended.state is TurnState.INTERRUPTED
    assert message_from_document((await store.messages_of(user.id, sid))[-1]).failed
    assert left.session_id not in store._sessions
    here._now = jumped(timedelta(days=2))
    assert (await here.sweep()).events == 7
    await gone.close()
    await here.close()


@asyncio_test
async def test_a_new_chat_refused_leaves_no_conversation_behind() -> None:
    controller = await over(MemoryStore())
    user = await controller.ensure_user(Identity("local", "me"))
    with pytest.raises(UnknownModelError):
        await controller.start_session(user, agent="echo", model="gone", text="one")
    await controller.drain()
    with pytest.raises(DrainingError):
        await controller.start_session(user, agent="echo", model="echo", text="one")
    assert (await controller.list_sessions(user, limit=10)).sessions == ()
    await controller.close()


class BusyStartStore(MemoryStore):
    async def start_turn(self, *args: Any, **kwargs: Any) -> None:
        raise BusyError("no connection was free")


@asyncio_test
async def test_a_first_turn_the_pool_had_no_room_for_takes_its_conversation_back() -> None:
    store = BusyStartStore()
    controller = await over(store)
    user = await controller.ensure_user(Identity("local", "me"))
    with pytest.raises(BusyError):
        await controller.start_session(user, agent="echo", model="echo", text="one")
    assert (await controller.list_sessions(user, limit=10)).sessions == ()
    assert store._sessions == {}
    await controller.close()


@asyncio_test
async def test_a_conversation_with_no_message_opens() -> None:
    store = MemoryStore()
    controller = await over(store)
    user = await controller.ensure_user(Identity("local", "me"))
    now = datetime.now(UTC)
    empty = Session(uuid.uuid4(), user.id, "echo", "echo", now, now)
    await store.add_session(empty)
    opened = await controller.open_session(user, empty.id)
    assert (opened.messages, opened.active) == ((), None)
    await controller.close()


class BusyFinishStore(MemoryStore):
    """A pool with no connection free for a turn's finish, twice."""

    def __init__(self) -> None:
        super().__init__()
        self.refused = 0

    async def finish_turn(self, *args: Any, **kwargs: Any) -> None:
        if self.refused < 2:
            self.refused += 1
            raise BusyError("no connection was free")
        await super().finish_turn(*args, **kwargs)


@asyncio_test
async def test_a_finish_the_pool_had_no_room_for_is_written_again() -> None:
    busy = BusyFinishStore()
    controller = await over(busy)
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="one")
    await asyncio.wait_for(settled(controller, user, started), 10.0)
    assert busy.refused == 2
    turn = await busy.get_turn(user.id, started.session_id, started.turn_id)
    assert turn is not None
    assert turn.state is TurnState.FINISHED
    answer = (await controller.open_session(user, started.session_id)).messages[-1]
    assert not answer.failed
    await controller.close()


class FlakyAppendStore(MemoryStore):
    """A database that drops the connection under the second batch of a turn, once."""

    def __init__(self) -> None:
        super().__init__()
        self.batches = 0

    async def append_events(self, *args: Any, **kwargs: Any) -> None:
        self.batches += 1
        if self.batches == 2:
            raise ConnectionResetError("the connection went")
        await super().append_events(*args, **kwargs)


@asyncio_test
async def test_a_batch_written_in_the_background_that_failed_is_held_and_written_later() -> None:
    flaky = FlakyAppendStore()
    controller = await over(flaky)
    engine = ChattyEngine()
    controller._engines["echo"] = engine
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="count")
    sid = started.session_id
    await until_async(lambda: flaky.last_position(user.id, sid, started.turn_id), lambda n: n >= 3)
    engine.gate.set()
    await settled(controller, user, started)
    stored = await flaky.events_after(user.id, sid, started.turn_id, 0)
    assert [p for p, _ in stored] == list(range(1, len(stored) + 1))
    text = "".join(
        e.text
        for e in (event_from_document(d).event for _, d in stored)
        if isinstance(e, TextPiece)
    )
    assert text == "one two three four five"
    await controller.close()


@asyncio_test
async def test_a_cancel_from_another_process_before_the_runner_began_ends_the_turn() -> None:
    store = MemoryStore()
    first = await over(store, worker="pod-a")
    loading = asyncio.Event()

    async def slow_engine(name: str) -> Any:
        await loading.wait()
        return EchoEngine()

    second = await over(store, worker="pod-b")
    user = await first.ensure_user(Identity("local", "me"))
    started = await first.start_session(user, agent="echo", model="echo", text="one")
    await settled(first, user, started)
    first._engine = slow_engine  # type: ignore[method-assign]
    # A second turn whose runner is still building its engine when the cancel arrives.
    turn = await first.regenerate_answer(
        user, started.session_id, question_id=started.question.id, model="echo"
    )
    await asyncio.sleep(0.05)
    assert await second.cancel_turn(user, started.session_id, turn.turn_id) is True
    ended = await store.get_turn(user.id, started.session_id, turn.turn_id)
    assert ended is not None
    assert ended.state is TurnState.CANCELLED
    await first.close()
    await second.close()


@asyncio_test
async def test_a_turn_too_late_to_run_fails_at_once_rather_than_wait_for_its_lease() -> None:
    # A deadline the configuration would refuse, as a runner that began late would find.
    store = MemoryStore()
    config = dataclasses.replace(CONFIG, work=WorkConfig(max_turn_seconds=5.0))
    controller = await over(store, config=config)
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="one")
    watched = [e async for e in controller.watch_turn(user, started.session_id, started.turn_id)]
    assert watched[-1].event == TurnEnded(TurnState.FAILED)
    turn = await store.get_turn(user.id, started.session_id, started.turn_id)
    assert turn is not None
    assert (turn.state, turn.error) == (TurnState.FAILED, "its deadline had passed before it began")
    await controller.close()


@asyncio_test
async def test_a_conversation_the_sweep_cannot_purge_does_not_stop_the_others() -> None:
    store = MemoryStore()
    controller = await over(store)
    user = await controller.ensure_user(Identity("local", "me"))
    now = datetime.now(UTC)
    gone = Session(uuid.uuid4(), user.id, "echo", "an-engine-no-longer-here", now, now)
    kept = Session(uuid.uuid4(), user.id, "echo", "echo", now, now)
    for each in (gone, kept):
        await store.add_session(each)
        await store.hide_session(user.id, each.id, now - timedelta(hours=1))
    swept = await controller.sweep()
    assert swept.sessions_purged == 1
    assert set(store._sessions) == {gone.id}
    await controller.close()
