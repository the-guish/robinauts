# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""What every ``Agent`` must do: the events of a turn, the memory, failure, cancellation.

The suite both adapters are held to (``docs/specs/agents.md``), written
against the fake, so that the LangChain and the Pydantic AI adapters are held
to one description rather than to whatever each of them happened to do. The
order itself is not restated here: it is ``core.check_backend_events``, which
is the same statement the application is written against.

**Subclass it and override ``new_agent``.** What the suite hands you is a
``Script`` -- what the model is to say, turn by turn of its loop, which tools
it asks for and what those answer, and how the turn is to end -- and what it
expects back is an ``Agent`` whose model does that and whose provider is never
reached. For the real adapters that means the framework's own test model
scripted with it, and the tools as plain functions the framework runs
(exactly as the examples hand them over); for the fake it is the script turned
into steps. The suite never says how, and asks nothing about the framework.

``definition``, ``prompt`` and ``model_id`` are overridable too: an adapter
that must be handed its own model id says so there. What is **not**
overridable is any of the promises below.

Two declarations say what a turn of this adapter **can be scripted into**,
and neither weakens a promise: ``can_answer_without_streaming`` is whether it
can be asked for an answer with no text delta in it at all, and
``can_call_tools`` whether its model can be scripted to ask for one.

The situations, which are the ones the application distinguishes:

- an answer that is **streamed**: its text in deltas, then ``Done`` with the
  same text;
- an answer that is **not**: no delta at all, ``Done`` with the whole of it.
  Not every provider streams, and an adapter is never required to;
- a **tool round**: the model asks for a tool, the framework runs it, the
  result comes back and the model answers -- as ``ToolCall``, ``ToolResult``
  and the answer's deltas, in that order, and ``Done`` last;
- the **memory**: a finished turn hands back a state, and a turn handed that
  state runs;
- a **failure**: the adapter raises, in the middle of an answer, and what it
  yielded stands as far as it got (``cut_short=True``);
- a **cancellation**: the task is cancelled, the ``CancelledError`` comes
  through, and it comes through **promptly**. An adapter that swallowed one
  would leave a run nothing can stop.

And, after every one of them, that the adapter **holds nothing** (``held``).
Letting a cancellation through is half the promise; the other half is the
response, the client or the connection that the turn opened, which the
``finally`` of the generator releases when the stream is closed.

There is no test here about ids, positions, storing or publishing: an adapter
has none of those, which is the whole point of the port.
"""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from dataclasses import dataclass, field
from enum import StrEnum

import pytest

from aio import asyncio_test
from conversations import agent_definition
from robinauts.legacy.core import check_backend_events
from robinauts.legacy.domain import AgentDefinition, Done, Event, TextDelta, ToolCall, ToolResult
from robinauts.legacy.ports import Agent

RELEASE_SECONDS = 5.0
"""How long a cancelled turn has to let the cancellation through.

Generous: it is not a measurement, it is the difference between "promptly"
and "never", and a test that hung would say the same thing hours later.
"""

PROMPT = "What is a robinaut?"


class Ending(StrEnum):
    """How the turn the script describes comes to an end."""

    COMPLETE = "complete"
    """The last answer is done and the turn returns."""
    FAIL = "fail"
    """The last answer is left open and the adapter raises."""
    HANG = "hang"
    """The last answer is left open and nothing more ever happens."""


@dataclass(frozen=True, slots=True)
class Call:
    """One tool the model is to ask for, and what the tool answers."""

    call_id: str
    name: str
    arguments: dict[str, object] = field(default_factory=dict)
    output: str = "found 3"


@dataclass(frozen=True, slots=True)
class Say:
    """One answer of the model's, in one step of the framework's loop."""

    text: str
    streamed: bool = True
    """Whether it arrives in deltas. An adapter that streams is done with
    exactly what it streamed; one that does not may be done with anything."""
    calls: tuple[Call, ...] = ()
    """The tools it asks for, which the framework runs before the next ``Say``."""


SEARCH = "search"
"""The one tool a scripted turn is handed, by the name the model is shown it under."""


@dataclass(frozen=True, slots=True)
class Script:
    """What the model says in one turn, step by step, and how the turn ends."""

    answers: tuple[Say, ...] = field(default_factory=tuple)
    ending: Ending = Ending.COMPLETE


class AgentContract:
    """Subclass this and override ``new_agent``."""

    can_answer_without_streaming: bool = True
    """Whether a turn of this adapter can be scripted to yield no text delta.

    ``False`` for an adapter whose framework hands the answer over in pieces
    however the provider sent it: what is then streamed is one piece, which is
    streaming. The half of the promise that still holds -- and that is checked
    -- is that what it streamed is what it is done with.
    """

    can_call_tools: bool = True
    """Whether a turn of this adapter can be scripted to ask for a tool.

    ``False`` for an adapter that binds none, whose model can therefore not
    be scripted to call one; it says which of the port's situations this
    adapter produces and weakens nothing it does produce.
    """

    def new_agent(self, script: Script) -> Agent:
        """An adapter whose model says that, and which reaches no provider."""
        raise NotImplementedError("an AgentContract subclass overrides `new_agent`")

    def held(self, agent: Agent) -> int:
        """How much that adapter still holds open: a response, a client, a connection.

        Zero when it has let go of everything. **Required**, because "an
        adapter releases what it holds" is the promise a cancelled run depends
        on and it cannot be checked from outside: only the implementation
        knows what it opened.

        It may be **engine-wide** rather than per turn -- a deployment shares
        one adapter between every conversation, so "is anything still open" is
        the question an implementation can cheaply answer. The suite asks it
        only between turns, and runs one turn at a time, which is when that
        answer and "is *this* turn still open" are the same answer.
        """
        raise NotImplementedError("an AgentContract subclass overrides `held`")

    def definition(self) -> AgentDefinition:
        """The agent it is asked to run. Override for an adapter that needs its own."""
        return agent_definition()

    def model_id(self) -> str:
        """The model it is told to run on: the agent's default, unless overridden.

        A keyword of its own and not read off the agent, because the model is
        the run's (``robinauts.legacy.ports.agents``); an adapter's own suite is where
        a turn on a model other than the agent's default is looked at.
        """
        return self.definition().model

    def prompt(self) -> str:
        """The question it is asked."""
        return PROMPT

    async def turn(self, script: Script, *, state: bytes | None = None) -> list[Event]:
        """Every event of one turn, run to its end."""
        agent = self.new_agent(script)
        seen: list[Event] = []
        events = agent.stream(self.definition(), self.prompt(), model=self.model_id(), state=state)
        # The application closes the stream to release what the adapter holds,
        # so the stream must be closeable: that is part of the port.
        assert hasattr(events, "aclose")
        async with aclosing(events):
            async for event in events:
                seen.append(event)
        assert self.held(agent) == 0
        return seen

    # --- what a turn produces ---

    @asyncio_test
    async def test_a_streamed_answer_is_streamed_and_done(self) -> None:
        seen = await self.turn(Script(answers=(Say("Someone who plays fair."),)))

        check_backend_events(seen)
        assert any(isinstance(event, TextDelta) for event in seen)
        assert _done(seen).text == "Someone who plays fair."

    @asyncio_test
    async def test_an_answer_that_was_not_streamed_is_done_all_the_same(self) -> None:
        # Not every provider streams, and a turn through a path that does not
        # is a turn like any other.
        seen = await self.turn(Script(answers=(Say("All of it at once.", streamed=False),)))

        check_backend_events(seen)
        if self.can_answer_without_streaming:
            assert not [event for event in seen if isinstance(event, TextDelta)]
        assert _done(seen).text == "All of it at once."

    @asyncio_test
    async def test_what_was_streamed_is_what_the_turn_is_done_with(self) -> None:
        seen = await self.turn(Script(answers=(Say("A whole answer, streamed."),)))

        streamed = "".join(event.text for event in seen if isinstance(event, TextDelta))
        assert streamed == _done(seen).text

    # --- the memory ---

    @asyncio_test
    async def test_a_finished_turn_hands_back_a_state_the_next_turn_takes(self) -> None:
        """The conversation's memory is the framework's, in its own format:
        what one turn is done with is what the next is handed, and the
        platform never reads it (``docs/specs/conversations.md``)."""
        first = await self.turn(Script(answers=(Say("Someone who plays fair."),)))
        state = _done(first).state
        assert isinstance(state, bytes)
        assert state

        second = await self.turn(Script(answers=(Say("And a robin sings."),)), state=state)

        check_backend_events(second)
        assert _done(second).text == "And a robin sings."
        assert isinstance(_done(second).state, bytes)

    # --- a turn that calls a tool ---

    @asyncio_test
    async def test_a_tool_round_is_the_call_its_result_and_the_answer_after_them(self) -> None:
        """The framework runs the tool: the call is announced whole, the
        result comes back, and the model's answer follows -- and the turn is
        done once, last (``docs/specs/runs.md``)."""
        if not self.can_call_tools:
            pytest.skip("this adapter binds no tools")
        made = Call("toolu_01", SEARCH, {"q": "robinauts"}, output="found 3")
        seen = await self.turn(
            Script(answers=(Say("Let me look.", calls=(made,)), Say("Found three.")))
        )

        check_backend_events(seen)
        (called,) = [event for event in seen if isinstance(event, ToolCall)]
        assert (called.call_id, called.name, dict(called.arguments)) == (
            made.call_id,
            made.name,
            made.arguments,
        )
        (answered,) = [event for event in seen if isinstance(event, ToolResult)]
        assert (answered.call_id, answered.name, answered.output) == (
            made.call_id,
            made.name,
            "found 3",
        )
        assert seen.index(called) < seen.index(answered)
        assert _done(seen).text == "Found three."
        assert (
            "Found three."
            in "".join(
                event.text for event in seen[seen.index(answered) :] if isinstance(event, TextDelta)
            )
            or not self.can_answer_without_streaming
        )

    @asyncio_test
    async def test_a_turn_whose_model_asks_for_no_tool_announces_none(self) -> None:
        seen = await self.turn(Script(answers=(Say("Someone who plays fair."),)))

        assert not any(isinstance(event, ToolCall | ToolResult) for event in seen)

    # --- how a turn ends badly ---

    @asyncio_test
    async def test_a_failure_is_reported_by_raising_and_leaves_the_turn_undone(self) -> None:
        agent = self.new_agent(Script(answers=(Say("As far as it g"),), ending=Ending.FAIL))
        seen: list[Event] = []

        with pytest.raises(Exception) as failure:  # noqa: B017 - an adapter raises what it likes
            async with aclosing(
                agent.stream(self.definition(), self.prompt(), model=self.model_id(), state=None)
            ) as events:
                async for event in events:
                    seen.append(event)

        assert not isinstance(failure.value, asyncio.CancelledError)
        # What it yielded stands as far as it got: a turn never done is what a
        # turn cut short looks like.
        check_backend_events(seen, cut_short=True)
        assert not any(isinstance(event, Done) for event in seen)
        assert self.held(agent) == 0

    @asyncio_test
    async def test_a_cancelled_turn_lets_the_cancellation_through_promptly(self) -> None:
        agent = self.new_agent(Script(answers=(Say("Half of an ans"),), ending=Ending.HANG))
        seen: list[Event] = []
        arrived = asyncio.Event()

        async def watch() -> None:
            async with aclosing(
                agent.stream(self.definition(), self.prompt(), model=self.model_id(), state=None)
            ) as events:
                async for event in events:
                    seen.append(event)
                    arrived.set()

        turn = asyncio.create_task(watch())
        # Cancelled while it is really running, not before it began: no sleep
        # decides that, the first event does.
        await arrived.wait()
        turn.cancel()

        async with asyncio.timeout(RELEASE_SECONDS):
            with pytest.raises(asyncio.CancelledError):
                await turn
        check_backend_events(seen, cut_short=True)
        # Let go of, not merely stopped: the promise a cancelled run depends
        # on is that the response, the client and the connection go with it.
        assert self.held(agent) == 0


def _done(events: list[Event]) -> Done:
    """The one event that ends the turn."""
    (done,) = [event for event in events if isinstance(event, Done)]
    return done
