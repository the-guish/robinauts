# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors

"""An agent engine: the seam between the platform and an agent framework, conversations included.

The port that is to replace ``robinauts.ports.agents.Agent``, which stays as it
is until this one is settled (``docs/working-notes/adapter-persistence-plan.md``).
What changes is who keeps the conversation. Under the old port the framework
hands its memory back as bytes at the end of every turn and is handed them
again at the next; under this one **the engine keeps its conversations
itself**, in storage of its own, and the platform keeps of the memory one
thing: the id of a checkpoint, an opaque token the engine hands back when a
turn finishes, stored on the answer that ended the turn. That is the contract
`agent-framework-examples
<https://github.com/the-guish/agent-framework-examples>`_ reached on
``feature/event-streaming-b`` -- an ``AgentBackend`` is one conversation in
one framework, its framework keeps the state between turns in storage the
backend owns, and the caller's record names the backend, the model and the
conversation id -- with one addition, so that a conversation can be forked
and a turn can be continued from where the platform knows it stands.

**An engine is more than an agent.** It runs the operator's agents, one turn
at a time; it keeps every conversation those turns build, in its framework's
own format, in tables of its own in the deployment's one database, from the
moment the platform creates one until the platform deletes it; it copies one
into another at a checkpoint, which is a fork; it reaches the model
providers its framework has a client for; and it releases what a turn
opened. An ``AgentDefinition`` -- the system prompt and the tool servers --
is what it is handed to run a turn *with*; the engine is what runs it.

**A checkpoint is the memory at the end of a finished turn, and nothing
else.** A turn makes whatever intermediate state its framework makes -- a
LangGraph checkpoint per step, a history after each model call -- and none of
it is the platform's business: none of it is stored by the platform, named by
the platform, or kept by the engine past the turn on the platform's account.
The one point the platform knows is the end of a turn, which the engine names
in ``Done.checkpoint_id``. What the id is, is the engine's: LangGraph's own
checkpoint id for the checkpoint the turn ended on, as it is; an id the
Pydantic AI adapter mints for the snapshot of the history it wrote. The
platform stores it as bounded text (``domain.MAX_CHECKPOINT_ID_CHARS``),
never parses it, and hands it back to the engine that produced it and to no
other. An engine keeps, for every checkpoint id it handed back, enough to
give that memory back, for as long as the conversation exists or a retention
rule says otherwise.

**The platform says where a turn continues from.** ``stream`` is handed the
checkpoint of the conversation's last stored answer, and the turn continues
from that memory, whatever else the engine holds for the conversation. So
when the engine finished a turn and the platform never received it -- a
crash between the engine's write and the platform's -- the next turn does not
continue from the orphan: the engine continues from the checkpoint it was
given, and whatever it holds past it is partial progress the platform never
had, which the engine may discard. The transcript is the authority on where
a conversation stands; the engine never assumes "the latest".

**Unless the platform asks to resume.** A turn that was interrupted -- the
process died, the task was cancelled, the engine raised -- may have left
partial work past the checkpoint: a loop half way through its tool calls, as
the framework's own per-step state holds it. For a chat that work is not
worth keeping, and the next turn starts from the checkpoint. For a task that
had run for two hours unattended it is, so a caller re-running the same turn
may ask for it with ``resume``, and the engine takes the turn up from where
its own state left it rather than from the checkpoint. What is resumed is
the engine's to keep: a framework with a checkpoint per step resumes from
the last one; one that keeps nothing of a turn before its end starts the
turn again, which is still correct, only longer.

**A conversation is created on purpose, and never by a turn.** ``create``
is the one call that makes a conversation exist in the engine; ``stream``
refuses one that was not created, as the examples' runner refuses to resume
what was never stored. A turn that could create what it was asked to
continue would let a caller with the wrong id -- a typo, a stale record, a
conversation already deleted -- start a conversation nobody asked for and
answer into it, and the platform would never know. The refusal is where the
mistake is found.

**Flat, not a handle.** Every call names the conversation it is about, and
nothing of a conversation is held in the engine between calls: a run outlives
the request that started it and may be taken up by another process, so
whatever a turn needs is read from the engine's storage when the turn begins
and is written there as the turn goes. The examples open a backend object per
conversation and close it; here the same lifetime is the one call that uses
it, ``stream``, whose generator releases on its way out. One object per
engine, built once by the composition root and shared by every conversation,
as the old port's was.

**What crosses.** In: the **conversation** by its id; the **checkpoint** to
continue from, by its id, or none for a conversation's first turn; the
**agent** as the operator has it now, read afresh every turn; the **prompt**,
the text of the question being answered; the **model**, by the platform's id
for it, which is the run's and not the agent's. Out: ``domain.events`` --
more text, more thinking, a tool call, its result, and ``Done`` with the
final answer and the new checkpoint's id -- which carry no ids of the
platform's, no times and no provenance. **No memory crosses**, in either
direction: a checkpoint id is a name for one, handed out by the engine and
handed back as it was.

**The framework owns the loop, the context and the memory** (ADR 0005). The
model asks for a tool, the framework calls it, the result goes back and the
model answers, as many times as the turn needs, and the engine translates
what it sees. What the model is sent of the history -- how much, summarised
how, with which cache breakpoints -- is the framework's middleware over the
framework's own history. The platform keeps a transcript for people and never
feeds the model from it; the engine never reads the transcript.

**The memory is the engine's, where the engine keeps it.** Written as the
turn goes, by the framework's own means or the engine's. Deleted with the
conversation (``forget``). Never inspected, exported or migrated by the
platform: the transcript is the durable record, the memory is the framework's
cache of it, and an engine that cannot read what it once wrote reports a
failed turn rather than reading it as nothing.

**Forking is copying the memory as it was at a checkpoint** (``fork``;
``docs/specs/conversations.md``, "Forking"). The platform names the
checkpoint from the answer the person forked from, and the engine makes the
new conversation exist with that memory and nothing after it. **The
source's earlier checkpoints stay valid in the fork**: the platform copies
the transcript up to the answer, checkpoint ids and all, so the engine copies
the memories those ids name along with the one forked at, under the same
ids, and the fork can then be continued, forked or cut back at any of them
exactly as the source could. The two are independent from then on.

**How a turn ends.**

- normally: the last event is ``Done``, once, with every call the turn
  announced answered and the id of the checkpoint the turn ended on. The
  checkpoint is written before ``Done`` is yielded, so a conversation whose
  turn finished is remembered whatever the platform then does with the
  transcript -- and if the platform does nothing with it, the id is never
  stored, and the next turn is told to continue from the one before.
- by **raising**: any exception ends the turn. The application records the run
  ``failed`` with a description of what was raised and leaves the message that
  was in flight uncompleted. An engine yields nothing after an error, and
  **what the turn said is not in the memory the next turn runs with**: the
  next turn continues from the checkpoint it is given, which is the one the
  platform had -- unless it asks to ``resume`` this very turn.
- by **cancellation**: the application cancels the task the iteration runs in,
  and closes the stream. An engine must not swallow ``CancelledError``; it
  lets it through, and what it holds -- the framework's run, the HTTP
  response, the connections to the tool servers -- is released by the
  ``finally`` of the generator, which closing runs. The same promise about the
  memory holds as for a turn that raised.

**Every refusal, and its error**, the same ones the conversation store uses
for the same mistakes (``robinauts.ports.conversations``):

- a conversation created twice, or forked onto one already created:
  ``InvalidValueError``, as an id already stored is -- a bug in the caller,
  not something to overwrite;
- a turn of, or a fork from, a conversation that was not created or was
  forgotten: ``ConversationNotFoundError``, raised where the stream is
  iterated for a turn;
- a checkpoint id the engine does not hold for that conversation -- another
  conversation's, a turn that did not finish, another engine's, since
  forgotten: ``CheckpointNotFoundError``, raised where the stream is iterated
  for a turn, and nothing is done with it;
- a ``resume`` whose prompt is not the one the partial work was answering:
  ``InvalidValueError``, raised where the stream is iterated, and nothing is
  run -- resuming is for the turn that was interrupted, never for the next;
- forgetting what is not there: nothing. Deleting is idempotent.

**What is not here.** How an engine is built (the composition root's
business: the pool, the models, the keys, the servers and their secrets, and a
storage kind for the tests); cutting a conversation back in place, which is
a turn continued from an earlier checkpoint and needs nothing more of the
port; and a turn that suspends on a tool call and resumes, which stands on
the per-step state this port leaves to the engine and changes the promise
above for one run state when it comes.

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
    async def create(self, conversation_id: uuid.UUID) -> None:
        """Make that conversation exist in this engine, with no memory yet.

        The one way a conversation comes to exist here. Called by the
        application when it creates the platform's record, **after** that
        record is stored: the engine's tables reference it, and a create for
        a conversation the platform does not have is the database's refusal
        (``ConversationNotFoundError``). If this call fails after the record
        was stored, the platform holds a conversation this engine does not
        know, which ``exists`` says and the application mends by calling this
        again before the first turn; nothing is answered into it meanwhile.

        Also what the application calls when an agent has been moved to this
        engine and a conversation of it arrives with a transcript and no
        memory here (``docs/specs/agents.md``, "A conversation stays with its
        engine"): said in the log, then created, then begun from nothing. The
        engine is not asked to know the difference.

        A conversation already created here is a bug in the caller, not
        something to start over: ``InvalidValueError``, and the memory stays
        as it was. What "exists with no memory" is in the engine's own terms
        -- a row, an empty history, a thread with nothing in it -- is the
        engine's.
        """
        raise NotImplementedError

    @abstractmethod
    async def exists(self, conversation_id: uuid.UUID) -> bool:
        """Whether this engine has that conversation.

        True from ``create`` or ``fork`` until ``forget``, whether or not a
        turn has run; false for one never created here, which is what a
        conversation moved to this engine from another looks like, and what
        a create that failed half way leaves. Asked and answered on its own,
        not inside ``stream``, so that the application can say in the log
        that a memory is missing before it answers without one.
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
        checkpoint_id: str | None,
        resume: bool = False,
    ) -> AsyncGenerator[Event, None]:
        """Send ``prompt`` as the next turn of that conversation and yield the turn's events.

        ``conversation_id`` names a conversation ``create`` or ``fork`` made.
        **A turn creates nothing**: one the engine does not have is
        ``ConversationNotFoundError``, raised where the stream is iterated,
        and nothing is written for it.

        ``checkpoint_id`` is where the turn continues from: the id the
        engine handed back with the conversation's last stored answer, as
        the platform has it on that answer, or ``None`` for a conversation
        with no answer yet -- its first turn, or one moved to this engine
        from another. The turn runs on the memory as it was at that
        checkpoint and on nothing the engine may hold past it: a turn the
        engine finished and the platform never recorded, or one that was
        interrupted, is partial progress the platform never had, and a turn
        that does not ``resume`` lets the engine discard it, on this call or
        later, as it likes. An id this engine does not hold for the
        conversation is ``CheckpointNotFoundError``, raised where the stream
        is iterated, and nothing is written. The engine is not asked to
        remember what the last checkpoint was: the platform tells it, every
        turn.

        ``resume`` asks for the opposite: **take up the turn that was
        interrupted after that checkpoint**, from where the engine's own
        state left it, instead of starting it again. For the caller it is
        the same call as the one that was interrupted -- the same
        conversation, the same prompt, the same checkpoint -- made again;
        the engine does the rest. What it yields is what happens from the
        point resumed, not what was streamed before the interruption, which
        the caller already had; ``Done`` ends it as any turn, with a new
        checkpoint. Three cases, all of them a turn that runs to its end:

        - partial work of that turn is there: it is continued. A framework
          with a checkpoint per step goes on from the last one, its calls
          already answered not made again;
        - none is there -- the turn never began, or the engine keeps nothing
          of a turn before its end, or it was discarded by a turn that did
          not resume: the turn starts from the checkpoint, as without
          ``resume``. A caller may therefore always ask to resume when
          re-running an interrupted turn, and never has to know whether
          anything was kept;
        - the partial work there was answering another prompt:
          ``InvalidValueError``, and nothing is run. Resuming is for the
          turn that was interrupted; the next question of a conversation
          starts from the checkpoint, and a caller that passed ``resume``
          with it has a bug the engine refuses to run.

        Partial work is kept until a turn from the same checkpoint runs
        without ``resume``, or until ``forget``; how an engine keeps it is
        its own. ``False`` by default: a caller that gives it no thought gets
        the chat's behaviour, which discards.

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

        The turn ends with ``Done``, whose ``checkpoint_id`` is the engine's
        id for the memory as it now is -- after the question, the loop and
        the answer -- written before ``Done`` is yielded. The platform stores
        it on the answer and will hand it back here, as ``checkpoint_id``,
        for the next turn, and to ``fork``. It is never the one that was
        given: a turn that ran made a new checkpoint. ``None`` only from an
        engine that keeps no memory, which the platform then continues with
        ``None`` as well.

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
    async def fork(self, source_id: uuid.UUID, target_id: uuid.UUID, *, checkpoint_id: str) -> None:
        """Make ``target_id`` exist here with ``source_id``'s memory as it was at that checkpoint.

        ``create`` and a copy in one: afterwards ``exists(target_id)`` is
        true, its memory is what the source's was at ``checkpoint_id`` -- the
        answer that checkpoint ended on included, nothing of any later turn
        -- and the two are independent: a turn of either writes nothing the
        other reads, and forgetting either leaves the other whole. The source
        is not changed.

        ``checkpoint_id`` is the id on the answer the person forked from
        (``domain.Provenance``), handed back by this engine when that turn
        finished. Not held here for the source -- another conversation's, a
        turn that did not finish, since forgotten: ``CheckpointNotFoundError``,
        and nothing is made.

        **The source's checkpoints up to that one are valid in the target
        afterwards, under the same ids.** The platform copies the transcript
        up to the answer into the target, the ids on its answers included,
        and a fork is then a conversation like any other: its first turn
        continues from ``checkpoint_id``, and it can be forked, or continued
        from an earlier answer, at any id a copied answer carries. The engine
        copies what those ids name along with the memory forked at; what it
        holds for the source past the checkpoint is not copied.

        Called after the platform's record of the target is stored, as
        ``create`` is, and with the source's turn over: a fork taken while
        the source has a turn running copies a memory that is still being
        written, so the application refuses the request until the run has
        ended. ``source_id`` not created here, or forgotten:
        ``ConversationNotFoundError``. ``target_id`` already created here:
        ``InvalidValueError``, and its memory stays as it was.
        """
        raise NotImplementedError

    @abstractmethod
    async def forget(self, conversation_id: uuid.UUID) -> None:
        """Delete whatever this engine keeps for that conversation.

        Called when the conversation is deleted, before the platform's own
        record goes (``docs/specs/privacy.md``): if this fails, nothing is
        deleted and the person is told; if the record's deletion then fails,
        a conversation this engine no longer has is left, which ``exists``
        says and no turn can run into, and nothing is left behind unowned.
        Called again for a conversation that is already gone, or was never
        here, it does nothing and raises nothing: deleting is idempotent, as a
        retry needs it to be.

        A fork of the conversation is untouched: what was copied into it is
        its own.

        Must not be called while a turn of the conversation is running; the
        application ends the run first, as it does before deleting the
        record.
        """
        raise NotImplementedError
