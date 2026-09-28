# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""Running one turn: the seam between the platform and an agent framework.

**One method, because a turn is one thing** (``docs/specs/agents.md``): given
the agent the operator defined, the model the turn runs on and the history to
answer, stream what the model said. Everything else about a turn -- which
ids the messages get, what they hang under, what is written down, what a
watcher is told -- belongs to the application, which is why none of it is in
this signature.

**What crosses.** In: an ``AgentDefinition`` (the system prompt and the
engine, read afresh every turn, because editing an agent takes effect at the
next turn of its existing conversations); the **model**, by the platform's id
for it, which is the run's and not the agent's -- the agent's model is only
the default a conversation starts with, and the conversation's may have been
changed since (``docs/specs/agents.md``); a **history**: the **full** visible
path from a root to the user message being answered, never trimmed above the
port; and the **tools** the run has, fetched once for the run from the servers
the agent names (``docs/specs/agents.md``, "Tools"). Out: ``EngineEvent``s,
which carry no ids of the platform's, no times and no provenance, because an
engine has none.

**What of the history the model sees is the adapter's to decide** (ADR 0004):
ordering, trimming and every other kind of context management, and prompt
caching, are per framework and per vendor, because a real policy counts the
vendor's tokens and places the vendor's cache breakpoints. The one invariant
kept above the port, and checked in each adapter's own tests, is that the
question being answered is whole in what the model sees.

**Both engines are stateless per turn** (ADR 0002). Nothing is remembered
between calls: the conversation record is the whole of the state, and the
history handed in is where a turn starts from, whichever engine ran the turn
before it.

**How it ends.**

- normally: the last event is an ``AnswerCompleted`` or, when that answer
  asked for tools, the ``WaitingOnTools`` after it. A turn produces at least
  one answer; one that produces none is a failed run
  (``docs/specs/runs.md``), and the application is what records that.
- by **raising**: any exception ends the turn. The application records the run
  ``failed`` with a description of what was raised and leaves the answer that
  was in flight uncompleted. An engine yields nothing after an error.
- by **cancellation**: the application cancels the task the iteration runs in,
  and closes the stream. An engine must not swallow ``CancelledError``; it
  lets it through, and what it holds is released by the ``finally`` of the
  generator, which closing runs.

**Waiting on tool calls.** A turn ends either "finished" or "waiting on these
tool calls" (``docs/specs/runs.md``). The second is an answer completed with
``ToolCallPart``s -- each announced, its arguments streamed and completed on
the way -- followed by ``WaitingOnTools`` and by nothing else: **an engine
never executes a tool**. The application runs the calls, appends their results
as one tool message and starts the next engine turn from the stored history,
so an engine sees a tool round as an ordinary turn whose history ends in a
tool message rather than in a question.

The contract suite both engines are held to is
``backend/tests/contracts/agents.py``, and the order it holds them to is
``robinauts.core.check_engine_events``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, Sequence

from robinauts.domain import AgentDefinition, EngineEvent, Message, ProviderKind, ToolDefinition


class Agent(ABC):
    """One engine, able to run one turn of one agent."""

    kinds: frozenset[ProviderKind] = frozenset()
    """The model provider kinds this engine has a client for.

    **Not every model exists under every engine** (``docs/specs/agents.md``),
    and not every provider's client passes the dependency policy at a given
    version (``DEPENDENCIES.md``). So an engine says what it can reach, and
    the composition root asks it rather than knowing: the configuration is
    then refused at start-up, naming the provider, instead of a person
    waiting for an answer from a client that was never built.

    Declared here and not in an adapter so that asking costs the root no
    second name from a framework's sub-package -- the one import and the one
    construction are what deleting an adapter must break, and nothing else
    (``docs/layout.md``, the discard test).

    Empty by default, which is a test double's honest answer: a scripted
    engine reaches no provider at all, and a deployment wired with one
    configures no model provider either.
    """

    @abstractmethod
    def run_turn(
        self,
        agent: AgentDefinition,
        history: Sequence[Message],
        tools: Sequence[ToolDefinition],
        *,
        model: str,
    ) -> AsyncGenerator[EngineEvent, None]:
        """Answer ``history`` as ``agent`` on ``model`` with ``tools``, streaming the turn.

        ``history`` is the visible path of the conversation ending in the
        **user message being answered** or, inside a tool round, in the
        **tool message** holding the results the model is to go on from; it
        is never empty and never ends anywhere else. It is the whole path:
        what of it the model sees is this engine's to decide (ADR 0004). The
        system prompt is ``agent``'s and is not one of the messages
        (``docs/specs/conversations.md``).

        ``tools`` is the list the run fetched once and holds for the turn,
        sorted by name and bounded (``docs/specs/agents.md``, "Tools");
        empty for an agent that names no server. The engine binds them to the
        model as they are and executes none of them.

        ``model`` is the id of the model the **run** records
        (``domain.Run.model``), never ``agent.model``: that is the agent's
        default, and a conversation may run on another. An id the engine's
        configuration does not have is a failure of the turn
        (``domain.UnknownModelError``), raised where the stream is iterated,
        like any other.

        Not a coroutine: it hands back the stream, which is then iterated.

        **An async generator, and that is part of the port.** What it hands
        back must have ``aclose()``, because closing the stream is how the
        application releases what the engine holds -- when the turn ends,
        when it fails, and above all when the task running it is cancelled,
        which is how a run is cancelled. An implementation is therefore an
        ``async def`` with ``yield``s in it, whose ``finally`` releases the
        HTTP response, the client or the file; the contract suite asks the
        object for ``aclose`` and asks the engine what it still holds
        afterwards.

        The close is **bounded** by the application: an engine that takes too
        long to let go is abandoned rather than allowed to hold up the run
        that has already ended.
        """
        raise NotImplementedError
