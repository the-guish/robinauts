# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""An agent engine: the seam between the platform and an agent framework, conversations included.

The port that is to replace ``robinauts.ports.agents.Agent``, which stays as it
is until this one is settled (``docs/working-notes/adapter-persistence-plan.md``).
What changes is who keeps the conversation. Under the old port the framework
hands its memory back as bytes at the end of every turn and is handed them
again at the next; under this one **the engine keeps its conversations
itself**, in storage of its own, and the platform knows a conversation by its
id and nothing else. That is the contract `agent-framework-examples
<https://github.com/the-guish/agent-framework-examples>`_ reached on
``feature/event-streaming-b``: an ``AgentBackend`` is one conversation in one
framework, its framework keeps the state between turns in storage the backend
owns, and the caller's record names the backend, the model and the
conversation id -- and nothing of the state.

**An engine is more than an agent.** It runs the operator's agents, one turn
at a time; it keeps every conversation those turns build, in its framework's
own format, in tables of its own in the deployment's one database; it reaches
the model providers its framework has a client for; and it releases what a
turn opened. An ``AgentDefinition`` -- the system prompt and the tool servers
-- is what it is handed to run a turn *with*; the engine is what runs it.

**Flat, not a handle.** Every call names the conversation it is about, and
nothing of a conversation is held in the engine between calls: a run outlives
the request that started it and may be taken up by another process, so
whatever a turn needs is read from the engine's storage when the turn begins
and is written there as the turn goes. The examples open a backend object per
conversation and close it; here the same lifetime is the one call that uses
it, ``stream``, whose generator releases on its way out. One object per
engine, built once by the composition root and shared by every conversation,
as the old port's was.

**What crosses.** In: the **conversation** by its id; the **agent** as the
operator has it now, read afresh every turn; the **prompt**, the text of the
question being answered; the **model**, by the platform's id for it, which is
the run's and not the agent's. Out: ``domain.events`` -- more text, more
thinking, a tool call, its result, and ``Done`` with the final answer -- which
carry no ids of the platform's, no times and no provenance. **No memory
crosses**, in either direction.

**The framework owns the loop, the context and the memory** (ADR 0005). The
model asks for a tool, the framework calls it, the result goes back and the
model answers, as many times as the turn needs, and the engine translates
what it sees. What the model is sent of the history -- how much, summarised
how, with which cache breakpoints -- is the framework's middleware over the
framework's own history. The platform keeps a transcript for people and never
feeds the model from it; the engine never reads the transcript.

**The memory is the engine's, where the engine keeps it.** Written as the
turn goes, by the framework's own means or the engine's: a checkpoint per
step, or the history after the run. Deleted with the conversation
(``forget``). Never inspected, exported or migrated by the platform: the
transcript is the durable record, the memory is the framework's cache of it,
and an engine that cannot read what it once wrote reports a failed turn rather
than reading it as nothing.

**How a turn ends.**

- normally: the last event is ``Done``, once, with every call the turn
  announced answered. What the turn said is in the memory before ``Done`` is
  yielded, so a conversation whose turn finished is remembered whatever the
  platform then does with the transcript.
- by **raising**: any exception ends the turn. The application records the run
  ``failed`` with a description of what was raised and leaves the message that
  was in flight uncompleted. An engine yields nothing after an error, and
  **what the turn said is not in the memory the next turn runs with**: the
  conversation resumes from the memory it had.
- by **cancellation**: the application cancels the task the iteration runs in,
  and closes the stream. An engine must not swallow ``CancelledError``; it
  lets it through, and what it holds -- the framework's run, the HTTP
  response, the connections to the tool servers -- is released by the
  ``finally`` of the generator, which closing runs. The same promise about the
  memory holds as for a turn that raised.

**What is not here.** How an engine is built (the composition root's
business: the pool, the models, the keys, the servers and their secrets, and a
storage kind for the tests); cutting a conversation back to an earlier message
(rewind) and copying one to a new id (fork), which come with the features that
need them; and a turn that suspends on a tool call and resumes, which stands
on the per-step memory this port allows and changes the promise above for one
run state when it comes.

The contract suite every engine is held to is to be
``backend/tests/contracts/agent_engines.py``, and the order it holds them to
is ``robinauts.core.check_backend_events``.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from typing import ClassVar

from robinauts.domain import AgentDefinition, Event, ProviderKind


class AgentEngine(ABC):
    """One agent framework as the platform sees it: runs the agents, keeps the conversations."""

    kinds: ClassVar[frozenset[ProviderKind]] = frozenset()
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

    schema_sql: ClassVar[str] = ""
    """The SQL that creates this engine's tables, or empty for an engine that needs none.

    An engine keeps its conversations in tables of its own in the
    deployment's one database (``docs/specs/backend.md``). It ships the DDL
    for them here, and ``robinauts db init`` applies it after the platform's
    own file, in the same transaction and under the same pin, so that a
    database is complete or it is nothing. The server never applies it.

    What the DDL must keep to: names prefixed with the engine's, so that two
    engines never collide in one schema; no schema named, so that the whole
    thing lands wherever ``search_path`` points; and every table keyed by the
    conversation, with ``conversation_id uuid NOT NULL REFERENCES
    conversations (id) ON DELETE CASCADE`` -- the one place an engine names a
    table of the platform's -- so that a conversation purged below the
    application takes its memory with it. ``forget`` is the application's
    way; the cascade is the database's backstop.

    Class-level, so that the command can read it without building an engine,
    which would need keys it has no business asking for.
    """

    @abstractmethod
    async def exists(self, conversation_id: uuid.UUID) -> bool:
        """Whether this engine holds a memory for that conversation.

        True once a turn of it has finished here and until ``forget``; false
        for a conversation it has never run, which is what the first turn
        finds and what a conversation moved to this engine from another
        finds -- a transcript with messages and no memory, which the
        application notes in the log and begins from nothing, once
        (``docs/specs/agents.md``, "A conversation stays with its engine").

        Asked and answered on its own, not inside ``stream``: a turn does not
        need it, and the application asks before a turn so that the loss is
        said before the question is answered without it.
        """
        raise NotImplementedError

    @abstractmethod
    def stream(
        self,
        conversation_id: uuid.UUID,
        agent: AgentDefinition,
        prompt: str,
        *,
        model: str,
    ) -> AsyncGenerator[Event, None]:
        """Send ``prompt`` as the next turn of that conversation and yield the turn's events.

        ``conversation_id`` names the conversation whose memory the turn
        continues and extends. One the engine has no memory for begins a
        conversation; the engine creates whatever it keeps under the id and
        asks nothing of the platform about it.

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

        Not a coroutine: it hands back the stream, which is then iterated.
        Nothing is done here -- building the model, listing the tools,
        reading the memory, opening the stream -- all of it happens inside
        the generator, so that a provider that refuses a key and a database
        that is away are failures of the turn, reported by raising where the
        caller is iterating, and not exceptions thrown at whoever asked for
        the stream.

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

        **Two turns of one conversation never overlap**: the platform allows
        a conversation one active run, so an engine may assume that no
        other turn is writing the same memory while this one runs. Turns of
        different conversations run side by side, through the one engine.
        """
        raise NotImplementedError

    @abstractmethod
    async def forget(self, conversation_id: uuid.UUID) -> None:
        """Delete whatever this engine keeps for that conversation.

        Called when the conversation is deleted, before the platform's own
        record goes (``docs/specs/privacy.md``): if this fails, nothing is
        deleted and the person is told; if the record's deletion then fails,
        a conversation without a memory is left, which the next turn begins
        from nothing, and nothing is left behind unowned. Called again for a
        conversation that is already gone, or was never here, it does nothing
        and raises nothing: deleting is idempotent, as a retry needs it to be.

        Must not be called while a turn of the conversation is running; the
        application ends the run first, as it does before deleting the
        record.
        """
        raise NotImplementedError
