# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""An engine a test writes the script for, step by step.

It is the ``Agent`` of ``robinauts.ports`` -- one method, streaming
``EngineEvent``s -- and what makes it useful is that **the test decides when
each step happens**. A script is a list of steps, and a step is one of three
things:

- an **engine event**, which is yielded;
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
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from robinauts.domain import (
    AgentDefinition,
    AnswerCompleted,
    AnswerReasoningDelta,
    AnswerStarted,
    AnswerTextDelta,
    EngineEvent,
    Message,
    MessagePart,
    ToolCallArgumentsDelta,
    ToolCallCompleted,
    ToolCallPart,
    ToolCallStarted,
    ToolDefinition,
    WaitingOnTools,
    text_parts,
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


Step = EngineEvent | Gate | Raise
"""What a script is made of."""


@dataclass(frozen=True, slots=True)
class Asked:
    """One turn, as the engine was asked for it."""

    agent: AgentDefinition
    history: tuple[Message, ...]
    model: str
    """The model it was told to run on: the run's, which is not always the agent's."""
    tools: tuple[ToolDefinition, ...] = ()
    """The tools it was handed for the turn."""


class ScriptedAgent(Agent):
    """An engine that yields what a test told it to, when the test lets it."""

    def __init__(self, *steps: Step) -> None:
        self.steps: list[Step] = list(steps)
        """The script. A test may replace it between turns."""
        self.next: list[list[Step]] = []
        """Scripts for the turns to come, one each, used up in order (``then``).

        A tool round is two turns of the engine inside one run: the answer
        that calls, and the answer after the results. The first runs
        ``steps``; the second runs the first of these, if a test wrote one,
        and ``steps`` again if not.
        """
        self.asked: list[Asked] = []
        """Every turn it was asked for, with the history it was given."""
        self.released = 0
        """How many turns had their iteration closed: what a released engine did."""
        self._open = 0

    def run_turn(
        self,
        agent: AgentDefinition,
        history: Sequence[Message],
        tools: Sequence[ToolDefinition],
        *,
        model: str,
    ) -> AsyncIterator[EngineEvent]:
        # Recorded here rather than inside the iteration: what a turn was asked
        # is true the moment it is asked, whether or not anybody iterates.
        self.asked.append(
            Asked(agent=agent, history=tuple(history), model=model, tools=tuple(tools))
        )
        script = self.next.pop(0) if self.asked[1:] and self.next else self.steps
        return self._events(tuple(script))

    def then(self, *steps: Step) -> None:
        """What the engine yields at the next turn it is asked for after this one."""
        self.next.append(list(steps))

    async def _events(self, steps: tuple[Step, ...]) -> AsyncIterator[EngineEvent]:
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
    def history(self) -> tuple[Message, ...]:
        """The history of the last turn it was asked for."""
        return self.asked[-1].history


def says(
    text: str,
    *,
    streamed: bool = True,
    reasoning: str = "",
    pieces: int = 1,
    extras: Mapping[str, Any] | None = None,
) -> list[Step]:
    """The ordinary steps of one answer: announced, streamed, completed.

    ``streamed=False`` is an engine that yields no text delta and completes
    with the whole answer, which is allowed: not every provider streams.
    ``pieces`` splits the text over that many deltas, for a test about what is
    published as it arrives. ``extras`` is what the engine hands back for the
    stored message to carry (``Message.extras``).
    """
    steps: list[Step] = [AnswerStarted()]
    if reasoning:
        steps.append(AnswerReasoningDelta(text=reasoning))
    if streamed and text:
        size = max(1, -(-len(text) // pieces))
        steps.extend(
            AnswerTextDelta(text=text[start : start + size]) for start in range(0, len(text), size)
        )
    steps.append(AnswerCompleted(parts=text_parts(text), extras=extras or {}))
    return steps


def calls(
    *made: tuple[str, str, Mapping[str, Any]],
    text: str = "",
    streamed: bool = True,
    extras: Mapping[str, Any] | None = None,
) -> list[Step]:
    """The steps of an answer that asks for tools, and the turn ending waiting.

    ``made`` is one ``(call_id, name, arguments)`` per call. Announced,
    streamed as text and then as calls -- each announced, its arguments
    streamed as JSON when ``streamed``, and completed -- completed with the
    text and the calls as parts, and followed by ``WaitingOnTools``, which is
    how a turn that asked for tools ends (``docs/specs/runs.md``).
    """
    steps: list[Step] = [AnswerStarted()]
    if text:
        steps.append(AnswerTextDelta(text=text))
    parts: list[MessagePart] = list(text_parts(text)) if text else []
    for call_id, name, arguments in made:
        call = ToolCallPart(call_id=call_id, name=name, arguments=arguments)
        steps.append(ToolCallStarted(call_id=call_id, name=name))
        if streamed:
            written = json.dumps(dict(call.arguments))
            half = len(written) // 2
            steps.append(ToolCallArgumentsDelta(call_id=call_id, text=written[:half]))
            steps.append(ToolCallArgumentsDelta(call_id=call_id, text=written[half:]))
        steps.append(ToolCallCompleted(call=call))
        parts.append(call)
    steps.append(AnswerCompleted(parts=tuple(parts), extras=extras or {}))
    steps.append(WaitingOnTools())
    return steps
