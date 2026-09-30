# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The scripted engine, against the contract both real adapters must pass.

The fake is the first implementation of the ``Agent`` port, so it is the first
thing the suite is run against -- which is also how the suite is shown to work
before there is an adapter to hold to it. A script is turned into steps here,
in the test, and not in the contract: what a ``Script`` means for an
implementation is that implementation's business.

The tests below are the fake's own promises, which the contract does not make
because no real adapter keeps them: the gates a test drives it with, the point
it raises at, and what it was asked.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import aclosing

import pytest

from aio import asyncio_test
from contracts.agents import AgentContract, Ending, Script
from conversations import MODEL, agent_definition
from fakes import Gate, Raise, ScriptedAgent, Step, calls, results, says
from robinauts.domain import Done, InvalidValueError, TextDelta
from robinauts.ports import Agent


def steps_for(script: Script) -> list[Step]:
    """A script as this engine's steps: the events, and how it ends."""
    steps: list[Step] = []
    for at, answer in enumerate(script.answers):
        last = at == len(script.answers) - 1
        if answer.streamed and answer.text:
            steps.append(TextDelta(text=answer.text))
        if answer.calls:
            steps.extend(
                calls(*((call.call_id, call.name, call.arguments) for call in answer.calls))
            )
            steps.extend(
                results(*((call.call_id, call.name, call.output) for call in answer.calls))
            )
        elif last and script.ending is Ending.COMPLETE:
            steps.append(Done(text=answer.text, state=json.dumps({"said": answer.text}).encode()))
        elif last:
            # Left open: streamed as far as it got, and then the engine either
            # raises or never comes back.
            steps.append(Raise() if script.ending is Ending.FAIL else Gate())
    if not script.answers:
        steps.append(
            Done(text="")
            if script.ending is Ending.COMPLETE
            else Raise() if script.ending is Ending.FAIL else Gate()
        )
    return steps


class TestScriptedAgent(AgentContract):
    """The fake, held to everything a real adapter is held to."""

    def new_agent(self, script: Script) -> Agent:
        return ScriptedAgent(*steps_for(script))

    def held(self, agent: Agent) -> int:
        assert isinstance(agent, ScriptedAgent)
        return agent.held


def streaming(agent: ScriptedAgent, *, state: bytes | None = None):  # type: ignore[no-untyped-def]
    return agent.stream(agent_definition(), "What is a robinaut?", model=MODEL, state=state)


@asyncio_test
async def test_a_gate_holds_the_turn_until_the_test_opens_it() -> None:
    gate = Gate()
    agent = ScriptedAgent(TextDelta(text="Before"), gate, *says("After the gate."))
    seen = []

    async def watch() -> None:
        async with aclosing(streaming(agent)) as events:
            async for event in events:
                seen.append(event)

    turn = asyncio.create_task(watch())
    await gate.reached.wait()

    assert seen == [TextDelta(text="Before")]
    assert not turn.done()

    gate.open()
    await turn

    assert isinstance(seen[-1], Done)


@asyncio_test
async def test_it_raises_where_the_script_says_and_not_before() -> None:
    failure = InvalidValueError("the provider refused")
    agent = ScriptedAgent(TextDelta(text="so far"), Raise(failure))
    seen = []

    with pytest.raises(InvalidValueError) as raised:
        async with aclosing(streaming(agent)) as events:
            async for event in events:
                seen.append(event)

    assert raised.value is failure
    assert seen == [TextDelta(text="so far")]


@asyncio_test
async def test_it_records_every_turn_with_what_it_was_given() -> None:
    agent = ScriptedAgent(*says("Answered."))

    async with aclosing(streaming(agent, state=b"{}")) as events:
        async for _ in events:
            pass

    assert agent.asked[-1].agent == agent_definition()
    assert agent.asked[-1].model == MODEL
    assert agent.asked[-1].state == b"{}"
    assert agent.prompt == "What is a robinaut?"
    assert agent.released == 1


@asyncio_test
async def test_a_turn_nobody_finishes_is_released_when_it_is_closed() -> None:
    # What a cancelled run does to an engine: the iteration is closed, and
    # whatever it held goes with it.
    agent = ScriptedAgent(TextDelta(text="Half"), Gate())

    async with aclosing(streaming(agent)) as events:
        assert await anext(events) == TextDelta(text="Half")
        assert agent.held == 1

    assert agent.released == 1
    assert agent.held == 0
