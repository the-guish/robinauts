# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The sweep: expired events and sign-in records go, turns past their lease end, and the
hidden sessions whose purge did not happen are purged."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from test_controller_recovery import DyingEngine
from test_controller_turns import opened, settled

from aio import asyncio_test
from robinauts.controller.application.housekeeping import Housekeeper
from robinauts.controller.contract.domain import (
    ApiToken,
    Identity,
    TurnState,
    UserSession,
)

LATER = timedelta(days=2)


def jumped(by: timedelta) -> Any:
    return lambda: datetime.now(UTC) + by


@asyncio_test
async def test_the_sweep_ends_a_dead_turn_and_deletes_what_has_expired() -> None:
    controller = await opened()
    engine = DyingEngine()
    controller._engines["echo"] = engine
    store = controller._store
    user = await controller.ensure_user(Identity("local", "me"))
    finished = await controller.start_session(user, agent="echo", model="echo", text="one")
    engine.dies = False
    await settled(controller, user, finished)
    engine.dies = True
    dead = await controller.start_session(user, agent="echo", model="echo", text="two")
    while len(await store.events_after(user.id, dead.session_id, dead.turn_id, 0)) < 6:
        await store.wait_for_events(user.id, dead.session_id, dead.turn_id, 0, 0.05)
    task, _ = controller._dispatcher._tasks[dead.turn_id]
    task.cancel("lost")
    now = datetime.now(UTC)
    hashed = hashlib.sha256(b"x").hexdigest()
    sign_ins = controller._sign_ins = _credentials(controller)
    await sign_ins.add_user_session(UserSession(uuid.uuid4(), user.id, hashed, now, now))
    await sign_ins.add_api_token(ApiToken(uuid.uuid4(), user.id, "t", hashed, now, now))

    controller._now = jumped(LATER)
    await controller.sweep()

    turn = await store.get_turn(user.id, dead.session_id, dead.turn_id)
    assert turn is not None
    assert turn.state is TurnState.INTERRUPTED
    answer = (await controller.open_session(user, dead.session_id)).messages[-1]
    assert answer.failed
    assert len(answer.parts) == 3
    # Every event was past its expiry, two days on.
    assert await store.events_after(user.id, finished.session_id, finished.turn_id, 0) == []
    assert await sign_ins.resolve_user_session(hashed, now - timedelta(days=1)) is None
    assert await sign_ins.api_tokens_of(user.id) == []
    await controller.close()


def _credentials(controller: Any) -> Any:
    from robinauts.controller.adapters.memory.credentials import MemoryCredentials

    return MemoryCredentials(controller._store)


@asyncio_test
async def test_the_sweep_purges_a_session_deleted_while_its_turn_would_not_end() -> None:
    controller = await opened()
    engine = DyingEngine()
    controller._engines["echo"] = engine
    store = controller._store
    user = await controller.ensure_user(Identity("local", "me"))
    started = await controller.start_session(user, agent="echo", model="echo", text="one")
    sid = started.session_id
    while len(await store.events_after(user.id, sid, started.turn_id, 0)) < 6:
        await store.wait_for_events(user.id, sid, started.turn_id, 0, 0.05)
    # The process running the turn died: the cancel reaches nobody.
    await controller._work_loop.stop()
    task, _ = controller._dispatcher._tasks.pop(started.turn_id)
    task.cancel("lost")
    from robinauts.controller.application import controller as controller_module

    controller_module.CANCEL_WAIT, waited = 0.1, controller_module.CANCEL_WAIT
    try:
        await controller.delete_session(user, sid)
    finally:
        controller_module.CANCEL_WAIT = waited
    assert sid in store._sessions
    assert await engine.exists(sid)
    await controller.sweep()
    assert sid in store._sessions
    controller._now = jumped(timedelta(hours=1))
    await controller.sweep()
    assert sid not in store._sessions
    assert not await engine.exists(sid)
    await controller.close()


@asyncio_test
async def test_the_housekeeper_sweeps_again_and_again_and_outlives_a_failure() -> None:
    swept: list[int] = []

    async def sweep() -> None:
        swept.append(1)
        if len(swept) == 1:
            raise RuntimeError("the database went away")

    keeper = Housekeeper(sweep, every=0.02)
    keeper.start()
    await asyncio.sleep(0.2)
    await keeper.stop()
    assert len(swept) >= 3
