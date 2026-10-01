# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What every ``AgentEngine`` must do, as ``docs/specs/agent-engines.md`` says.

Subclass ``EngineMemoryContract`` and override ``new_engine``, which returns an engine set up
and ready, whose ``MODEL`` answers without reaching anything.
"""

from __future__ import annotations

import uuid

import pytest

from aio import asyncio_test
from robinauts.agent_engines.contract.domain import (
    AgentDefinition,
    CheckpointNotFoundError,
    Done,
    SessionExistsError,
    SessionNotFoundError,
)
from robinauts.agent_engines.contract.ports import AgentEngine

AGENT = AgentDefinition(system_prompt="You are a robinaut.")


async def turn(
    engine: AgentEngine, session: uuid.UUID, model: str, after: str | None = None
) -> Done:
    events = [
        event
        async for event in engine.stream(
            session, AGENT, "hello", model=model, checkpoint_id=after, timeout_seconds=10.0
        )
    ]
    done = events[-1]
    assert isinstance(done, Done)
    return done


class EngineMemoryContract:
    MODEL = "m"

    async def new_engine(self) -> AgentEngine:
        raise NotImplementedError("an EngineMemoryContract subclass overrides `new_engine`")

    @asyncio_test
    async def test_a_session_created_twice_is_refused(self) -> None:
        engine = await self.new_engine()
        session = uuid.uuid4()
        await engine.create(session)
        with pytest.raises(SessionExistsError):
            await engine.create(session)
        assert await engine.exists(session)

    @asyncio_test
    async def test_a_turn_on_an_unknown_session_is_refused(self) -> None:
        engine = await self.new_engine()
        with pytest.raises(SessionNotFoundError):
            await turn(engine, uuid.uuid4(), self.MODEL)

    @asyncio_test
    async def test_a_checkpoint_of_another_session_is_refused(self) -> None:
        engine = await self.new_engine()
        session, other = uuid.uuid4(), uuid.uuid4()
        await engine.create(session)
        await engine.create(other)
        done = await turn(engine, session, self.MODEL)
        with pytest.raises(CheckpointNotFoundError):
            await turn(engine, other, self.MODEL, after=done.checkpoint_id)
        with pytest.raises(CheckpointNotFoundError):
            await turn(engine, session, self.MODEL, after="not-one")

    @asyncio_test
    async def test_the_next_turn_continues_from_a_handed_out_checkpoint(self) -> None:
        engine = await self.new_engine()
        session = uuid.uuid4()
        await engine.create(session)
        first = await turn(engine, session, self.MODEL)
        second = await turn(engine, session, self.MODEL, after=first.checkpoint_id)
        assert second.checkpoint_id != first.checkpoint_id
        await turn(engine, session, self.MODEL, after=first.checkpoint_id)  # an earlier one too

    @asyncio_test
    async def test_forget_is_safe_to_repeat(self) -> None:
        engine = await self.new_engine()
        session = uuid.uuid4()
        await engine.create(session)
        await turn(engine, session, self.MODEL)
        await engine.forget(session)
        await engine.forget(session)
        assert not await engine.exists(session)
        with pytest.raises(SessionNotFoundError):
            await turn(engine, session, self.MODEL)
