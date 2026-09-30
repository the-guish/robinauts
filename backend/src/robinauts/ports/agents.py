# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Running one turn: the seam between the platform and an agent framework.

**The contract is the examples'** (`agent-framework-examples
<https://github.com/the-guish/agent-framework-examples>`_, ``common/base.py``
on ``feature/event-streaming``): an ``AgentBackend`` is one conversation in
one framework, whose framework keeps the state between turns and runs the
whole turn -- tools included -- and ``stream(prompt)`` yields the turn's
events as they happen, in a form no framework defines. Here the same thing is
one method on one object per engine, made asynchronous and handed the
conversation's state rather than holding it, because a deployment runs many
conversations through one adapter and keeps their memory in its database
(``docs/specs/agents.md``, "The agent port").

**What crosses.** In: an ``AgentDefinition`` (the system prompt and the tool
servers it may use, read afresh every turn); the **prompt**, the text of the
question being answered; the **model**, by the platform's id for it, which is
the run's and not the agent's; and the **state**, the conversation as the
framework left it after the last finished turn, or ``None`` for a conversation
with no memory yet. Out: ``domain.events`` -- more text, more thinking, a tool
call, its result, and ``Done`` with the final answer and the state after this
turn -- which carry no ids of the platform's, no times and no provenance.

**The framework owns the loop, the context and the memory** (ADR 0005). The
model asks for a tool, the framework calls it, the result goes back and the
model answers, as many times as the turn needs, and the adapter translates
what it sees. What the model is sent of the history -- how much, summarised
how, with which cache breakpoints -- is the framework's middleware over the
framework's own history; the platform keeps a transcript for people and never
feeds the model from it. The one thing kept above the port is that the
question being answered is whole in what the model sees, which is what every
framework does with the turn it is answering.

**How it ends.**

- normally: the last event is ``Done``, once, with every call the turn
  announced answered. The application stores ``Done.state`` with the run's
  ending and hands it to the conversation's next turn.
- by **raising**: any exception ends the turn. The application records the run
  ``failed`` with a description of what was raised and leaves the message that
  was in flight uncompleted. An adapter yields nothing after an error, and
  the turn leaves no state: the conversation resumes from the memory it had.
- by **cancellation**: the application cancels the task the iteration runs in,
  and closes the stream. An adapter must not swallow ``CancelledError``; it
  lets it through, and what it holds -- the framework's run, the HTTP
  response, the connections to the tool servers -- is released by the
  ``finally`` of the generator, which closing runs.

The contract suite both adapters are held to is
``backend/tests/contracts/agents.py``, and the order it holds them to is
``robinauts.core.check_backend_events``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator

from robinauts.domain import AgentDefinition, Event, ProviderKind


class Agent(ABC):
    """One engine, able to run one turn of any of the operator's agents."""

    kinds: frozenset[ProviderKind] = frozenset()
    """The model provider kinds this engine has a client for.

    **Not every model exists under every engine** (``docs/specs/agents.md``),
    and not every provider's client passes the dependency policy at a given
    version (``DEPENDENCIES.md``). So an engine says what it can reach, and
    the composition root asks it rather than knowing: the configuration is
    then refused at start-up, naming the provider, instead of a person
    waiting for an answer from a client that was never built.

    Empty by default, which is a test double's honest answer: a scripted
    engine reaches no provider at all.
    """

    @abstractmethod
    def stream(
        self,
        agent: AgentDefinition,
        prompt: str,
        *,
        model: str,
        state: bytes | None,
    ) -> AsyncGenerator[Event, None]:
        """Send ``prompt`` as the next user turn and yield the turn's events as they happen.

        ``agent`` is the definition as the operator has it now: its system
        prompt is sent with every request and never enters the memory, so
        editing an agent takes effect at the next turn of its existing
        conversations; its ``tools`` name the servers the framework connects
        to for the turn (``docs/specs/agents.md``, "Tools").

        ``model`` is the id of the model the **run** records
        (``domain.Run.model``), never ``agent.model``: that is the agent's
        default, and a conversation may run on another. An id the engine's
        configuration does not have is a failure of the turn
        (``domain.UnknownModelError``), raised where the stream is iterated,
        like any other.

        ``state`` is what this engine's last finished turn of the conversation
        handed back in ``Done.state``, or ``None``. It is this engine's own
        serialisation of its history and is handed back as it was given; an
        engine is never given another engine's state (``docs/specs/agents.md``,
        "A conversation stays with its engine").

        Not a coroutine: it hands back the stream, which is then iterated.

        **An async generator, and that is part of the port.** What it hands
        back must have ``aclose()``, because closing the stream is how the
        application releases what the engine holds -- when the turn ends,
        when it fails, and above all when the task running it is cancelled,
        which is how a run is cancelled. An implementation is therefore an
        ``async def`` with ``yield``s in it, whose ``finally`` releases what
        the turn opened; the contract suite asks the object for ``aclose``
        and asks the engine what it still holds afterwards.

        The close is **bounded** by the application: an engine that takes too
        long to let go is abandoned rather than allowed to hold up the run
        that has already ended.
        """
        raise NotImplementedError
