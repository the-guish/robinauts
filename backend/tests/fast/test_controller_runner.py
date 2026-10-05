# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The runner and the worker: the claim, the lease, the parts, cancels, deletes, the close."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import create_autospec

import pytest
from echo_controller import CONFIG, start_echo_controller, wait_for_turn_end

from robinauts.agent_engines.contract.domain import Event
from robinauts.agent_engines.contract.ports import AgentEngine
from robinauts.agent_engines.echo_engine.engine import EchoEngine
from robinauts.controller.adapters.memory.credentials import MemoryCredentials
from robinauts.controller.adapters.memory.store import MemoryStore
from robinauts.controller.application.controller import RobinautsController
from robinauts.controller.application.worker import Worker
from robinauts.controller.composition import Composed
from robinauts.controller.contract.domain import (
    ActiveTurn,
    Identity,
    NumberedEvent,
    SessionNotFoundError,
    StorageConfig,
    StorageKind,
    TurnEnded,
    TurnStarted,
    TurnState,
    User,
)
from robinauts.controller.core.documents import event_from_document
from robinauts.web.lifecycle import Lifecycle
from util.aio import asyncio_test

FIVE_MINUTES = timedelta(minutes=5)


def gated_engine() -> tuple[Any, asyncio.Event]:
    """The echo engine, mocked: it streams the echo's turn once the gate is set."""
    echo = EchoEngine()
    gate = asyncio.Event()
    engine = create_autospec(AgentEngine, spec_set=True, instance=True)
    engine.kinds.side_effect = echo.kinds

    async def stream(*args: Any, **kwargs: Any) -> AsyncGenerator[Event, None]:
        await gate.wait()
        async for event in echo.stream(*args, **kwargs):
            yield event

    engine.stream.side_effect = stream
    return engine, gate


class SlowFinishStore(MemoryStore):
    async def finish_turn(self, *args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(0.1)
        await super().finish_turn(*args, **kwargs)


async def over(
    store: MemoryStore,
    close_timeout: float = 10.0,
    engines: dict[str, Any] | None = None,
    **options: Any,
) -> Lifecycle:
    """A controller over that store, wired as the composition wires one, and started."""
    controller = RobinautsController(CONFIG, store=store, **options)
    worker = Worker(
        store,
        CONFIG,
        storage=StorageConfig(StorageKind.IN_MEMORY),
        secret_for={}.get,
        engines=engines,
        close_timeout=close_timeout,
    )
    composed = Composed(controller, MemoryCredentials(store), worker)
    lifecycle = Lifecycle(composed)
    await lifecycle.start()
    return lifecycle


def shift_clock(by: timedelta) -> Any:
    return lambda: datetime.now(UTC) + by


async def gated_turn(
    **options: Any,
) -> tuple[Lifecycle, Any, Any, asyncio.Event, User, TurnStarted]:
    """A turn on a ``gated_engine``, started and held at the gate."""
    engine, gate = gated_engine()
    lifecycle = await over(MemoryStore(), engines={"echo": lambda *_: engine}, **options)
    controller: Any = lifecycle.composed.controller
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="one")
    await controller._store.wait_for_events(user.id, started.session_id, started.turn_id, 0, 5.0)
    return lifecycle, controller, engine, gate, user, started


@asyncio_test
async def test_a_runner_whose_claim_is_refused_runs_no_engine_and_never_finishes() -> None:
    lifecycle, controller, engine, gate, user, started = await gated_turn()
    sid = started.session_id
    opened_session = await controller.open_session(user, sid)
    assert opened_session.messages == (started.question,)
    active = opened_session.active
    assert isinstance(active, ActiveTurn)
    assert (active.turn_id, active.follows) == (started.turn_id, started.question.id)
    # A second runner for the same task, as a duplicate dispatch would be.
    turn = await controller._store.get_turn(user.id, sid, started.turn_id)
    assert turn is not None
    await lifecycle.composed.worker.run_task(await controller._store.get_task(turn.task_id))
    assert engine.stream.call_count == 1
    turn = await controller._store.get_turn(user.id, sid, started.turn_id)
    assert turn is not None
    assert turn.state is TurnState.ACTIVE
    gate.set()
    await wait_for_turn_end(controller, user, started)
    assert len(await controller._store.messages_of(user.id, sid)) == 2
    await lifecycle.stop()


@asyncio_test
async def test_a_runner_refused_mid_stream_writes_nothing_more() -> None:
    lifecycle, controller, engine, gate, user, started = await gated_turn()
    sid = started.session_id
    turn = await controller._store.get_turn(user.id, sid, started.turn_id)
    task = lifecycle.composed.worker._running[turn.task_id]
    controller._now = shift_clock(FIVE_MINUTES)
    assert (await controller.open_session(user, sid)).active is None
    gate.set()
    await task
    turn = await controller._store.get_turn(user.id, sid, started.turn_id)
    assert turn is not None
    assert turn.state is TurnState.INTERRUPTED
    assert len(await controller._store.events_after(user.id, sid, started.turn_id, 0)) == 1
    # The question, and the answer marked failed that the reader stored.
    assert len(await controller._store.messages_of(user.id, sid)) == 2
    await lifecycle.stop()


@asyncio_test
async def test_a_turn_whose_lease_has_passed_is_interrupted_and_a_new_turn_starts() -> None:
    lifecycle, controller, engine, gate, user, started = await gated_turn(event_wait_timeout=0.01)
    sid = started.session_id
    controller._now = shift_clock(FIVE_MINUTES)
    watched = [e async for e in controller.watch_turn(user, sid, started.turn_id)]
    assert [type(n.event).__name__ for n in watched] == ["MessageStarted", "TurnEnded"]
    assert watched[-1].event == TurnEnded(TurnState.INTERRUPTED)
    assert watched[-1].position == 1
    gate.set()
    again = await controller.regenerate_answer(
        user, sid, question_id=started.question.id, model="echo"
    )
    await wait_for_turn_end(controller, user, again)
    turn = await controller._store.get_turn(user.id, sid, again.turn_id)
    assert turn is not None
    assert turn.state is TurnState.FINISHED
    await lifecycle.stop()


@asyncio_test
async def test_a_cancel_before_the_runner_claimed_ends_the_turn_cancelled() -> None:
    lifecycle = await start_echo_controller()
    controller: Any = lifecycle.composed.controller
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="one")
    await controller.cancel_turn(user, started.session_id, started.turn_id)
    # Ended by the controller, not the runner: no event of its own; the record says how.
    assert (
        await controller._store.events_after(user.id, started.session_id, started.turn_id, 0) == []
    )
    watched = [e async for e in controller.watch_turn(user, started.session_id, started.turn_id)]
    assert watched == [NumberedEvent(0, TurnEnded(TurnState.CANCELLED))]
    await lifecycle.stop()


@asyncio_test
async def test_a_cancel_of_a_turn_another_process_runs_reaches_it() -> None:
    store = MemoryStore()
    engine, gate = gated_engine()
    first = await over(store, engines={"echo": lambda *_: engine})
    user = await first.composed.controller.ensure_user(Identity("local", "me"))
    started = await first.composed.controller.start_session(
        user, agent="echo", model="echo", text="one"
    )
    await store.wait_for_events(user.id, started.session_id, started.turn_id, 0, 5.0)
    # Started once the first has claimed the turn, so that the second's worker cannot.
    second = await over(store)
    await second.composed.controller.cancel_turn(user, started.session_id, started.turn_id)
    await wait_for_turn_end(second.composed.controller, user, started)
    turn = await store.get_turn(user.id, started.session_id, started.turn_id)
    assert turn is not None
    assert turn.state is TurnState.CANCELLED
    await first.stop()
    await second.stop()


@asyncio_test
async def test_close_interrupts_a_running_turn_and_keeps_an_answer_being_finished() -> None:
    lifecycle, controller, _, _, user, started = await gated_turn()
    sid = started.session_id
    lifecycle.composed.worker._close_timeout = 0.05
    await lifecycle.stop()
    turn = await controller._store.get_turn(user.id, sid, started.turn_id)
    assert turn is not None
    assert turn.state is TurnState.INTERRUPTED
    stored = await controller._store.events_after(user.id, sid, started.turn_id, 0)
    assert event_from_document(stored[-1][1]).event == TurnEnded(TurnState.INTERRUPTED)

    slow = SlowFinishStore()
    lifecycle = await over(slow, close_timeout=0.01)
    controller = lifecycle.composed.controller
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="one")
    await asyncio.sleep(0.02)
    await lifecycle.stop()
    turn = await slow.get_turn(user.id, started.session_id, started.turn_id)
    assert turn is not None
    assert turn.state is TurnState.FINISHED
    assert len(await slow.messages_of(user.id, started.session_id)) == 2


@asyncio_test
async def test_a_session_can_be_deleted_during_a_turn() -> None:
    lifecycle, controller, engine, gate, user, started = await gated_turn()
    sid = started.session_id
    await controller.delete_session(user, sid)
    with pytest.raises(SessionNotFoundError):
        await controller.open_session(user, sid)
    await lifecycle.stop()
