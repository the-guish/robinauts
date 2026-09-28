# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""The scripted engine, against the contract both real engines must pass.

The fake is the first implementation of the ``Agent`` port, so it is the first
thing the suite is run against -- which is also how the suite is shown to work
before there is an engine to hold to it. A script is turned into steps here,
in the test, and not in the contract: what a ``Script`` means for an
implementation is that implementation's business.

The tests below are the fake's own promises, which the contract does not make
because no real engine keeps them: the gates a test drives it with, the point
it raises at, and the history it was given.
"""

from __future__ import annotations

import asyncio
from contextlib import aclosing

import pytest

from aio import asyncio_test
from contracts.agents import AgentContract, Ending, Script
from conversations import MODEL, agent_definition, question
from fakes import Gate, Raise, ScriptedAgent, Step, calls, says
from robinauts.domain import AnswerCompleted, AnswerStarted, AnswerTextDelta, InvalidValueError
from robinauts.ports import Agent


def steps_for(script: Script) -> list[Step]:
    """A script as this engine's steps: the events, and how it ends."""
    steps: list[Step] = []
    for at, answer in enumerate(script.answers):
        last = at == len(script.answers) - 1
        if answer.calls:
            steps.extend(
                calls(
                    *((call.call_id, call.name, call.arguments) for call in answer.calls),
                    text=answer.text,
                    streamed=answer.streamed,
                )
            )
        elif last and script.ending is not Ending.COMPLETE:
            # Left open: announced, streamed as far as it got, and then the
            # engine either raises or never comes back.
            steps.append(AnswerStarted())
            if answer.streamed and answer.text:
                steps.append(AnswerTextDelta(text=answer.text))
            steps.append(Raise() if script.ending is Ending.FAIL else Gate())
        else:
            steps.extend(says(answer.text, streamed=answer.streamed))
    if not script.answers and script.ending is not Ending.COMPLETE:
        steps.append(Raise() if script.ending is Ending.FAIL else Gate())
    return steps


class TestScriptedAgent(AgentContract):
    """The fake, held to everything a real engine is held to."""

    def new_agent(self, script: Script) -> Agent:
        return ScriptedAgent(*steps_for(script))

    def held(self, agent: Agent) -> int:
        assert isinstance(agent, ScriptedAgent)
        return agent.held


@asyncio_test
async def test_a_gate_holds_the_turn_until_the_test_opens_it() -> None:
    gate = Gate()
    agent = ScriptedAgent(AnswerStarted(), gate, *says("After the gate.")[1:])
    seen = []

    async def watch() -> None:
        async with aclosing(
            agent.run_turn(agent_definition(), (question(),), (), model=MODEL)
        ) as events:
            async for event in events:
                seen.append(event)

    turn = asyncio.create_task(watch())
    await gate.reached.wait()

    assert seen == [AnswerStarted()]
    assert not turn.done()

    gate.open()
    await turn

    assert isinstance(seen[-1], AnswerCompleted)


@asyncio_test
async def test_it_raises_where_the_script_says_and_not_before() -> None:
    failure = InvalidValueError("the provider refused")
    agent = ScriptedAgent(AnswerStarted(), AnswerTextDelta(text="so far"), Raise(failure))
    seen = []

    with pytest.raises(InvalidValueError) as raised:
        async with aclosing(
            agent.run_turn(agent_definition(), (question(),), (), model=MODEL)
        ) as events:
            async for event in events:
                seen.append(event)

    assert raised.value is failure
    assert seen == [AnswerStarted(), AnswerTextDelta(text="so far")]


@asyncio_test
async def test_it_records_every_turn_with_the_history_it_was_given() -> None:
    agent = ScriptedAgent(*says("Answered."))
    asked = question("What is a robinaut?")

    async with aclosing(agent.run_turn(agent_definition(), (asked,), (), model=MODEL)) as events:
        async for _ in events:
            pass

    assert agent.asked[-1].agent == agent_definition()
    assert agent.asked[-1].model == MODEL
    assert agent.history == (asked,)
    assert agent.released == 1


@asyncio_test
async def test_a_turn_nobody_finishes_is_released_when_it_is_closed() -> None:
    # What a cancelled run does to an engine: the iteration is closed, and
    # whatever it held goes with it.
    agent = ScriptedAgent(AnswerStarted(), Gate())

    async with aclosing(
        agent.run_turn(agent_definition(), (question(),), (), model=MODEL)
    ) as events:
        assert await anext(events) == AnswerStarted()
        assert agent.held == 1

    assert agent.released == 1
    assert agent.held == 0
