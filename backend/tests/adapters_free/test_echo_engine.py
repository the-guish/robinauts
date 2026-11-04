# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The echo engine: the events of a turn, a poisoned turn, and that it keeps nothing."""

from __future__ import annotations

import uuid

import pytest

from robinauts.agent_engines.contract.domain import (
    AgentDefinition,
    Done,
    EngineError,
    TextDelta,
    ToolCall,
    ToolResult,
)
from robinauts.agent_engines.echo_engine.engine import ANSWER, POISONED, TOOL, EchoEngine
from util.aio import asyncio_test

AGENT = AgentDefinition(system_prompt="")


async def turn(prompt: str, after: str | None = None) -> list[object]:
    events = []
    async for event in EchoEngine().stream(
        uuid.uuid4(), AGENT, prompt, model="m", checkpoint_id=after, timeout_seconds=1.0
    ):
        events.append(event)
    return events


@asyncio_test
async def test_a_turn_calls_the_tool_and_answers_the_fixed_string_plus_its_result() -> None:
    call, result, *said, done = await turn("hello", after="any checkpoint at all")
    assert isinstance(call, ToolCall)
    assert (call.name, call.raw_input) == (TOOL, {"text": "hello"})
    assert result == ToolResult(call_id=call.call_id, name=TOOL, raw_output="hello")
    assert said == [TextDelta(text=ANSWER), TextDelta(text="hello")]
    assert isinstance(done, Done)
    assert done.text == ANSWER + "hello"
    assert done.checkpoint_id != (await turn("hello"))[-1].checkpoint_id


@asyncio_test
async def test_a_poisoned_prompt_gets_an_error_from_the_tool_and_ends_the_turn_badly() -> None:
    events: list[object] = []
    with pytest.raises(EngineError, match=POISONED):
        async for event in EchoEngine().stream(
            uuid.uuid4(), AGENT, "poison me", model="m", checkpoint_id=None, timeout_seconds=1.0
        ):
            events.append(event)
    call, result = events
    assert isinstance(call, ToolCall)
    assert result == ToolResult(call_id=call.call_id, name=TOOL, raw_output=POISONED, failed=True)
