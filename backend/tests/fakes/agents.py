# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""An engine a test writes the script for, step by step.

It is the ``Agent`` of ``robinauts.ports`` -- one method, streaming the
adapter's events (``robinauts.domain.events``) -- and what makes it useful is
that **the test decides when each step happens**. A script is a list of
steps, and a step is one of three things:

- an **event**, which is yielded;
- a **gate**, which the engine waits at until the test opens it. That is how a
  test stops a turn in the middle, looks at what is stored, and lets it go on.
  A gate that is never opened is an engine that hangs, which is what
  cancellation and the turn timeout are tested against;
- a **raise**, which is how an engine reports a failure
  (``docs/specs/agents.md``). Anywhere in the script: before an answer, in the
  middle of one, after one.

**Nothing sleeps.** A gate is an ``asyncio.Event`` the test sets, and
``reached`` is another that the *engine* sets when it arrives, so a test waits
for the turn to be exactly where it wants it instead of guessing how long that
takes.

**It lets cancellation through**, as the port requires, and records that it was
released: ``released`` counts the turns whose iteration was closed and ``held``
what is still open, which is what "the engine releases what it holds" looks
like from outside.

**It remembers nothing between turns**, exactly as a real adapter does: what
it was handed as ``state`` is recorded (``Asked.state``) and what it hands back
is whatever the script's ``Done`` carries, so a test about the memory says in
its script what the engine returns and reads off ``asked`` what it was given.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from robinauts.domain import (
    AgentDefinition,
    Done,
    Event,
    ReasoningDelta,
    TextDelta,
    ToolCall,
    ToolResult,
)
from robinauts.ports import Agent


class Gate:
    """A point in a script the engine waits at until the test lets it past.

    ``reached`` is set by the engine when it arrives and awaited by the test;
    ``open`` is set by the test and awaited by the engine. Two events rather
    than one, so that "the turn is here" and "carry on" cannot be confused --
    and so that a gate nobody opens is simply an engine that never finishes.
    """

    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self._open = asyncio.Event()

    async def wait(self) -> None:
        self.reached.set()
        await self._open.wait()

    def open(self) -> None:
        """Let the turn go on from here."""
        self._open.set()


@dataclass(frozen=True, slots=True)
class Raise:
    """The step where the engine reports a failure, by raising."""

    error: BaseException = field(default_factory=lambda: RuntimeError("the provider said no"))


Step = Event | Gate | Raise
"""What a script is made of."""


@dataclass(frozen=True, slots=True)
class Asked:
    """One turn, as the engine was asked for it."""

    agent: AgentDefinition
    prompt: str
    model: str
    """The model it was told to run on: the run's, which is not always the agent's."""
    state: bytes | None = None
    """The memory it was handed: what the last finished turn left, or nothing."""


class ScriptedAgent(Agent):
    """An engine that yields what a test told it to, when the test lets it."""

    def __init__(self, *steps: Step) -> None:
        self.steps: list[Step] = list(steps)
        """The script. A test may replace it between turns."""
        self.next: list[list[Step]] = []
        """Scripts for the turns to come, one each, used up in order (``then``)."""
        self.asked: list[Asked] = []
        """Every turn it was asked for, with the prompt and the state it was given."""
        self.released = 0
        """How many turns had their iteration closed: what a released engine did."""
        self._open = 0

    def stream(
        self,
        agent: AgentDefinition,
        prompt: str,
        *,
        model: str,
        state: bytes | None,
    ) -> AsyncIterator[Event]:
        # Recorded here rather than inside the iteration: what a turn was asked
        # is true the moment it is asked, whether or not anybody iterates.
        self.asked.append(Asked(agent=agent, prompt=prompt, model=model, state=state))
        script = self.next.pop(0) if self.asked[1:] and self.next else self.steps
        return self._events(tuple(script))

    def then(self, *steps: Step) -> None:
        """What the engine yields at the next turn it is asked for after this one."""
        self.next.append(list(steps))

    async def _events(self, steps: tuple[Step, ...]) -> AsyncIterator[Event]:
        # What a real engine opens on its first step: an HTTP response, a
        # client, a file. It is counted so that a test can ask what the engine
        # still holds, which is what the contract's release check reads.
        self._open += 1
        try:
            for step in steps:
                if isinstance(step, Gate):
                    await step.wait()
                elif isinstance(step, Raise):
                    raise step.error
                else:
                    yield step
        finally:
            # Reached when the turn ends, when it raises, and when the
            # iteration is closed -- which is what a cancellation does.
            self._open -= 1
            self.released += 1

    @property
    def held(self) -> int:
        """How many turns of this engine are still holding something open.

        Zero once every turn has ended or been closed. A turn that was
        abandoned rather than closed leaves one, which is exactly what the
        contract's release check is looking for.
        """
        return self._open

    @property
    def prompt(self) -> str:
        """The prompt of the last turn it was asked for."""
        return self.asked[-1].prompt


def says(
    text: str,
    *,
    streamed: bool = True,
    reasoning: str = "",
    pieces: int = 1,
    state: bytes | None = b"{}",
) -> list[Step]:
    """The ordinary steps of one answer: thought about, streamed, done.

    ``streamed=False`` is an engine that yields no text delta and hands the
    whole answer over in ``Done``, which is allowed: not every provider
    streams. ``pieces`` splits the text over that many deltas, for a test about
    what is published as it arrives. ``state`` is what the engine hands back
    as the conversation's memory; something rather than nothing by default,
    so that a turn that finished is one the next turn resumes from.
    """
    steps: list[Step] = []
    if reasoning:
        steps.append(ReasoningDelta(text=reasoning))
    if streamed and text:
        size = max(1, -(-len(text) // pieces))
        steps.extend(
            TextDelta(text=text[start : start + size]) for start in range(0, len(text), size)
        )
    steps.append(Done(text=text, state=state))
    return steps


def calls(*made: tuple[str, str, Mapping[str, Any]], text: str = "") -> list[Step]:
    """The steps of an answer that asks for tools: its text, then each call.

    ``made`` is one ``(call_id, name, arguments)`` per call. The turn is not
    done: what follows is ``results`` for the calls, and then whatever the
    model said after them.
    """
    steps: list[Step] = [TextDelta(text=text)] if text else []
    steps.extend(
        ToolCall(call_id=call_id, name=name, arguments=arguments)
        for call_id, name, arguments in made
    )
    return steps


def results(*landed: tuple[str, str, str] | tuple[str, str, str, bool]) -> list[Step]:
    """The results of the calls announced before.

    One ``(call_id, name, output)`` each, with a fourth ``True`` for a result
    the tool marked as an error.
    """
    return [
        ToolResult(call_id=call_id, name=name, output=output, is_error=bool(flag and flag[0]))
        for call_id, name, output, *flag in landed
    ]
