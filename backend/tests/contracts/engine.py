# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What every ``AgentEngine`` must do, as ``docs/specs/agent-engines.md`` says.

Subclass ``EngineMemoryContract`` and override ``new_engine``, which returns an engine set up
and ready, whose ``MODEL`` answers without reaching anything.

Subclass ``EngineTurnContract`` and override ``new_engine(script)``, which returns an engine set
up and ready whose ``MODEL`` behaves as the ``Script`` says and is handed ``add`` as its tool,
with the settings of ``engine_settings.settings_for``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Coroutine
from enum import Enum
from typing import Any

import pytest

from engine_settings import MAX_MODEL_CALLS
from robinauts.agent_engines.contract.domain import (
    AgentDefinition,
    CheckpointNotFoundError,
    Done,
    Event,
    SessionExistsError,
    SessionNotFoundError,
    TextDelta,
    ToolCall,
    ToolResult,
)
from robinauts.agent_engines.contract.ports import AgentEngine

AGENT = AgentDefinition(system_prompt="You are a robinaut.")


def engine_test(test: Callable[..., Coroutine[Any, Any, None]]) -> Callable[..., None]:
    """Run the test on a loop of its own, and the suite's `release` after it, on the same
    loop: what a subclass whose engines hold a database needs."""

    def run(self: Any) -> None:
        async def go() -> None:
            try:
                await test(self)
            finally:
                await self.release()

        asyncio.run(go())

    run.__name__, run.__qualname__ = test.__name__, test.__qualname__
    run.__doc__, run.__module__ = test.__doc__, test.__module__
    return run


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

    async def release(self) -> None:
        """Let go of what the engines of this test held; nothing by default."""

    @engine_test
    async def test_a_session_created_twice_is_refused(self) -> None:
        engine = await self.new_engine()
        session = uuid.uuid4()
        await engine.create(session)
        with pytest.raises(SessionExistsError):
            await engine.create(session)
        assert await engine.exists(session)

    @engine_test
    async def test_a_turn_on_an_unknown_session_is_refused(self) -> None:
        engine = await self.new_engine()
        with pytest.raises(SessionNotFoundError):
            await turn(engine, uuid.uuid4(), self.MODEL)

    @engine_test
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

    @engine_test
    async def test_the_next_turn_continues_from_a_handed_out_checkpoint(self) -> None:
        engine = await self.new_engine()
        session = uuid.uuid4()
        await engine.create(session)
        first = await turn(engine, session, self.MODEL)
        second = await turn(engine, session, self.MODEL, after=first.checkpoint_id)
        assert second.checkpoint_id != first.checkpoint_id
        await turn(engine, session, self.MODEL, after=first.checkpoint_id)  # an earlier one too

    @engine_test
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


ANSWER = "Two and three make five."
CALL_ID = "call-1"
ARGUMENTS = {"a": 2, "b": 3}


def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


class ModelFailure(Exception):
    pass


class Script(Enum):
    ANSWER = "streams ANSWER in more than one piece"
    TOOL_ROUND = "calls add(**ARGUMENTS) as CALL_ID, then, given its result, streams ANSWER"
    FAIL = "raises ModelFailure"
    HANG = "never answers"
    TOOL_LOOP = "calls add(**ARGUMENTS) again after every result, for ever"


class EngineTurnContract:
    MODEL = "m"

    async def new_engine(self, script: Script) -> AgentEngine:
        raise NotImplementedError("an EngineTurnContract subclass overrides `new_engine`")

    async def release(self) -> None:
        """Let go of what the engines of this test held; nothing by default."""

    async def turn(self, script: Script, timeout_seconds: float = 10.0) -> list[Event]:
        engine = await self.new_engine(script)
        session = uuid.uuid4()
        await engine.create(session)
        stream = engine.stream(
            session,
            AGENT,
            "What are two and three?",
            model=self.MODEL,
            checkpoint_id=None,
            timeout_seconds=timeout_seconds,
        )
        return [event async for event in stream]

    @engine_test
    async def test_a_streamed_answer_is_its_pieces_and_done_once_last(self) -> None:
        events = await self.turn(Script.ANSWER)
        pieces = [event.text for event in events if isinstance(event, TextDelta)]
        assert len(pieces) > 1
        assert [type(event) for event in events].count(Done) == 1
        assert events[-1] == Done(text=ANSWER, checkpoint_id=events[-1].checkpoint_id)
        assert "".join(pieces) == ANSWER

    @engine_test
    async def test_a_tool_round_is_the_call_then_its_result_before_done(self) -> None:
        events = await self.turn(Script.TOOL_ROUND)
        call = events.index(ToolCall(CALL_ID, "add", ARGUMENTS))
        result = events.index(ToolResult(CALL_ID, "add", "5"))
        assert call < result < len(events) - 1
        assert isinstance(events[-1], Done)
        assert events[-1].text == ANSWER

    @engine_test
    async def test_a_failure_of_the_model_is_raised(self) -> None:
        with pytest.raises(ModelFailure):
            await self.turn(Script.FAIL)

    @engine_test
    async def test_a_cancellation_is_let_through(self) -> None:
        running = asyncio.create_task(self.turn(Script.HANG))
        await asyncio.sleep(0.2)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

    @engine_test
    async def test_a_turn_past_its_model_calls_ends_with_an_error(self) -> None:
        engine = await self.new_engine(Script.TOOL_LOOP)
        session = uuid.uuid4()
        await engine.create(session)
        events: list[Event] = []
        with pytest.raises(Exception) as raised:  # the framework's own error
            async for event in engine.stream(
                session,
                AGENT,
                "What are two and three?",
                model=self.MODEL,
                checkpoint_id=None,
                timeout_seconds=10.0,
            ):
                events.append(event)
        assert not isinstance(raised.value, TimeoutError | asyncio.CancelledError)
        calls = [event for event in events if isinstance(event, ToolCall)]
        assert 0 < len(calls) <= MAX_MODEL_CALLS
        assert not any(isinstance(event, Done) for event in events)

    @engine_test
    async def test_a_turn_past_its_timeout_ends_with_timeout_error(self) -> None:
        with pytest.raises(TimeoutError):
            await self.turn(Script.HANG, timeout_seconds=0.2)
