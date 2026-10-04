# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The echo engine keeps the contract: the events of a turn, the memory's ids, the refusals."""

from __future__ import annotations

import uuid

import pytest

from aio import asyncio_test
from contracts.engine import EngineMemoryContract
from robinauts.agent_engines.contract.domain import (
    AgentDefinition,
    CheckpointNotFoundError,
    Done,
    EngineError,
    ResumeMismatchError,
    SessionExistsError,
    SessionNotFoundError,
    TextDelta,
    ToolCall,
    ToolResult,
)
from robinauts.agent_engines.contract.ports import AgentEngine
from robinauts.agent_engines.echo_engine import engine as echo_engine_module
from robinauts.agent_engines.echo_engine.engine import ANSWER, POISONED, TOOL, EchoEngine

AGENT = AgentDefinition(system_prompt="")


async def turn(engine: EchoEngine, session: uuid.UUID, prompt: str, after: str | None = None):
    events = []
    async for event in engine.stream(
        session, AGENT, prompt, model="m", checkpoint_id=after, timeout_seconds=1.0
    ):
        events.append(event)
    return events


@asyncio_test
async def test_a_turn_calls_the_tool_and_answers_the_fixed_string_plus_its_result() -> None:
    engine = EchoEngine()
    session = uuid.uuid4()
    await engine.create(session)
    events = await turn(engine, session, "hello")
    assert events[0] == ToolCall(call_id="call-1", name=TOOL, arguments={"text": "hello"})
    assert events[1] == ToolResult(call_id="call-1", name=TOOL, output="hello")
    assert [e.text for e in events[2:-1] if isinstance(e, TextDelta)] == [ANSWER, "hello"]
    done = events[-1]
    assert isinstance(done, Done)
    assert done.text == ANSWER + "hello"
    assert done.checkpoint_id


@asyncio_test
async def test_the_next_turn_continues_from_a_checkpoint_it_handed_out() -> None:
    engine = EchoEngine()
    session = uuid.uuid4()
    await engine.create(session)
    first = (await turn(engine, session, "one"))[-1]
    second = (await turn(engine, session, "two", after=first.checkpoint_id))[-1]
    assert second.checkpoint_id != first.checkpoint_id
    assert second.text == ANSWER + "two"


@asyncio_test
async def test_a_poisoned_prompt_gets_an_error_from_the_tool_and_ends_the_turn_badly() -> None:
    engine = EchoEngine()
    session = uuid.uuid4()
    await engine.create(session)
    events = []
    with pytest.raises(EngineError, match=POISONED):
        async for event in engine.stream(
            session, AGENT, "poison me", model="m", checkpoint_id=None, timeout_seconds=1.0
        ):
            events.append(event)
    assert events == [
        ToolCall(call_id="call-1", name=TOOL, arguments={"text": "poison me"}),
        ToolResult(call_id="call-1", name=TOOL, output=POISONED, is_error=True),
    ]
    done = (await turn(engine, session, "hello"))[-1]  # the session goes on
    assert done.text == ANSWER + "hello"


@asyncio_test
async def test_the_call_id_counts_the_turns_the_checkpoint_remembers() -> None:
    engine = EchoEngine()
    session = uuid.uuid4()
    await engine.create(session)
    first = (await turn(engine, session, "one"))[-1]
    again = await turn(engine, session, "one, again")  # a branch from no checkpoint
    later = await turn(engine, session, "two", after=again[-1].checkpoint_id)
    deeper = await turn(engine, session, "two", after=first.checkpoint_id)
    assert [events[0].call_id for events in (again, later, deeper)] == [
        "call-1",
        "call-2",
        "call-2",
    ]


@asyncio_test
async def test_the_refusals_of_the_contract() -> None:
    engine = EchoEngine()
    session, other = uuid.uuid4(), uuid.uuid4()
    await engine.create(session)
    with pytest.raises(SessionExistsError):
        await engine.create(session)
    with pytest.raises(SessionNotFoundError):
        await turn(engine, other, "x")
    with pytest.raises(CheckpointNotFoundError):
        await turn(engine, session, "x", after="not-one")
    done = (await turn(engine, session, "same"))[-1]
    await engine.create(other)
    with pytest.raises(CheckpointNotFoundError):  # another session's checkpoint
        await turn(engine, other, "x", after=done.checkpoint_id)
    with pytest.raises(ResumeMismatchError):
        async for _ in engine.stream(
            session,
            AGENT,
            "different",
            model="m",
            checkpoint_id=None,
            timeout_seconds=1.0,
            resume=True,
        ):
            pass


@asyncio_test
async def test_fork_carries_the_checkpoints_up_to_the_one_asked_and_forget_drops_everything():
    engine = EchoEngine()
    source, target = uuid.uuid4(), uuid.uuid4()
    await engine.create(source)
    first = (await turn(engine, source, "a"))[-1]
    second = (await turn(engine, source, "b", after=first.checkpoint_id))[-1]
    await engine.fork(source, target, checkpoint_id=first.checkpoint_id)
    assert await engine.exists(target)
    await turn(engine, target, "c", after=first.checkpoint_id)  # the source's id, valid here
    with pytest.raises(CheckpointNotFoundError):  # but not the one after the fork point
        await turn(engine, target, "c", after=second.checkpoint_id)
    with pytest.raises(SessionExistsError):
        await engine.fork(source, target, checkpoint_id=first.checkpoint_id)
    await engine.forget(source)
    await engine.forget(source)  # safe to repeat
    assert not await engine.exists(source)
    with pytest.raises(SessionNotFoundError):
        await engine.fork(source, uuid.uuid4(), checkpoint_id=first.checkpoint_id)


class TestEchoEngineMemory(EngineMemoryContract):
    async def new_engine(self) -> AgentEngine:
        return EchoEngine()


@asyncio_test
async def test_a_slow_prompt_answers_a_word_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(echo_engine_module, "SLOW_PAUSE", 0.01)
    engine = EchoEngine()
    session = uuid.uuid4()
    await engine.create(session)
    events = [
        e
        async for e in engine.stream(
            session, AGENT, "slowly a b", model="m", checkpoint_id=None, timeout_seconds=1.0
        )
    ]
    texts = [e.text for e in events if isinstance(e, TextDelta)]
    assert texts == ["The tool said: ", "slowly ", "a ", "b"]
    assert isinstance(events[-1], Done)
    assert events[-1].text == "The tool said: slowly a b"
