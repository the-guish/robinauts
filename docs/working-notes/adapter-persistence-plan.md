# Plan: each adapter keeps its conversations; the platform keeps a transcript and an id

Written 2026-10-01 on `claude/nifty-cray-fa9ooe`, after
[memory-pointer-exploration.md](memory-pointer-exploration.md) and the
owner's decision that the persistence model of
[ADR 0005](../adr/0005-the-framework-owns-the-loop-and-the-memory.md) -- the
framework's state as bytes on the run -- is not acceptable, that a full
rewrite of the backend is admissible, and that the conversation may be
simplified for it, forks included. Progress against it will be in
`adapter-persistence-progress.md`. Not a spec: the decisions, what they cost
across the codebase, and the order to build it in. The spec sentences that
change are listed at the end.

The contract to build against is the one
[agent-framework-examples](https://github.com/the-guish/agent-framework-examples)
reached on `feature/event-streaming-b` (`d702815`): an `AgentBackend` is
**one conversation in one framework**, built by an async factory that does
its I/O first, whose framework keeps the conversation between turns in
storage the backend owns, and whose caller keeps a record naming the backend,
the model and the conversation id -- and nothing of the state.

## Why

ADR 0005 handed the loop, the context and the memory to the frameworks, and
then took the memory back as a blob: each adapter serialises its history at
the end of every turn, the platform stores it on the run in the same
transaction as the ending, and the next turn is handed it back. That gives
the platform one mechanism for two frameworks and a fork of the memory for
free, at these prices:

- every finished run stores the whole history again, so a conversation's
  memory grows with the square of its length until the framework
  summarises;
- a turn that did not end leaves nothing, because the one write is at the
  end: the model forgets a cancelled loop's calls and results;
- the frameworks' own persistence is unused -- LangGraph's checkpointer,
  which is what its interrupts, its resumption after a crash and a person's
  approval of a tool call stand on -- and the planned "tools that suspend a
  run" ([specs/core.md](../specs/core.md), "Planned") has nothing to stand
  on;
- the application finds the memory by walking the transcript's tree
  (`Turns._memory`), which ties the memory's location to the tree's shape,
  and the tree exists for a feature -- edits and regenerations with their
  lineage -- the owner is willing to drop.

The examples put the state where the framework wants it and keep the
caller's record to an id. This plan does the same here, in the one database
the deployment has.

## What is wanted

- The platform knows a conversation by its id, its agent, its model and its
  transcript. It stores nothing of the model's memory: not bytes, not a key.
- Each agent adapter keeps its conversations in storage it owns, in its
  framework's own way: LangGraph in a checkpointer, Pydantic AI in a table
  of message histories. Both in the deployment's one PostgreSQL, through the
  deployment's one connection pool.
- The port is the examples' second branch: a backend per conversation,
  opened by the engine for the turn and closed after it.
- A conversation is one line of messages. Edits and regenerations go, for
  now, and the tree goes with them.
- The transcript stays, for the people reading the conversation, the
  exports and retention, written from what the adapter streams; the model
  is never fed from it.
- Net lines deleted: the tree, the memory walk, the `engine_state`
  plumbing, the regenerate paths of the application, the wire and the
  interface; what comes in is a checkpointer of our own and two small
  storage modules.

## Settled

Ten decisions, made when this plan was written. Where a spec or the code
still says otherwise, this is the direction and the spec changes.

1. **The port is the examples' second branch, in two objects.** `Engine`
   replaces `ports.agents.Agent`: one per framework, built once by the
   composition root with the pool, the models, the keys, the tool servers
   and their secrets, and `await engine.open(conversation_id, agent, *,
   model) -> AgentBackend` is the examples' async factory -- it does its
   I/O first (LangGraph: nothing to load; Pydantic AI: read the history),
   so a provider that refuses a key and a database that is away are
   failures of the turn, where the caller awaits. `AgentBackend` is one
   conversation in one framework: `await exists()`, `stream(prompt)`, and
   `await close()`. **The stream stays an async generator** ending in
   `Done(text)`: the second branch's callback-per-event `ask` is a device
   for its subprocess pipe, while the generator is what gives this
   application backpressure (a provider faster than the database is held
   back, not buffered) and cancellation by `aclose()`, both proven. There
   is no `ask`, and no `set_model`: the model is the run's and is handed to
   `open`. A backend is opened by `execute` for the run and closed in its
   `finally`, every time; nothing of a conversation is held in a process
   between turns, because a run outlives its request and a turn may be
   taken up by another process.

2. **The key is the conversation id, and the platform keeps nothing else.**
   What the examples' caller stores -- the backend, the model, the id --
   the platform already has on the conversation and the run. `Done.state`,
   `MAX_ENGINE_STATE_BYTES`, `runs.engine_state`, `end_run(engine_state=)`,
   `engine_state(run_id)` and `Turns._memory` go. The adapter keeps whatever
   it keeps under the conversation id.

3. **Each adapter persists in the one database, in tables of its own,
   through the deployment's one pool.** The pool is handed to the engine at
   construction as a `Database` protocol declared in `ports` (`execute`,
   `fetch`, `fetchrow`, `executemany`; `asyncpg.Pool` satisfies it as it
   is), so the driver stays confined to `datastore` and the adapters write
   SQL and import no driver. Each adapter ships its DDL
   (`adapters/agents/<engine>/schema.sql`), and `robinauts db init` applies
   the platform's file and then every engine's, in the one transaction, with
   the one pin (`SCHEMA_SHA256`) taken over all of them. The tables are
   named by engine (`langgraph_checkpoints`, `langgraph_writes`,
   `pydantic_ai_histories`) in the same schema, so nothing names a schema
   and the tests' schema-per-test trick holds. Every table of theirs carries
   `conversation_id uuid NOT NULL REFERENCES conversations (id) ON DELETE
   CASCADE`: **deleting a conversation deletes its memory**, by the one
   mechanism there is, and the port has no `forget`. The adapter's DDL names
   the platform's table, which is written down and accepted. Each adapter
   has two storage kinds, as the examples' `StorageKind` does: the database
   for a deployment, and memory for its tests and the contract suite.

4. **LangGraph keeps its conversations in a checkpointer of our own.** A
   `BaseCheckpointSaver` over the `Database` protocol, written as a port of
   `langgraph-checkpoint-sqlite`'s `AsyncSqliteSaver` (MIT; 738 lines with
   its docstrings, two tables) to asyncpg's placeholders and `bytea`: the
   five async methods -- `aget_tuple`, `alist`, `aput`, `aput_writes`,
   `adelete_thread` -- over `langgraph_checkpoints (conversation_id,
   checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint
   bytea, metadata bytea)` and `langgraph_writes (conversation_id,
   checkpoint_ns, checkpoint_id, task_id, idx, channel, type, value bytea)`,
   with `JsonPlusSerializer`. The sync five raise, as an async-only saver's
   do; `langgraph-checkpoint` 4.2.0 declares none of them abstract and the
   delta-channel pair has a default. `langgraph-checkpoint-postgres` is
   still not adopted (`psycopg`, LGPL), and no new dependency comes in:
   `langgraph-checkpoint` is already in the tree and `InMemorySaver` ships
   with it for the tests. The thread is the conversation (`thread_id` =
   the id as text; stored as `uuid`), `create_agent(..., checkpointer=)` is
   built per turn as today over the engine's one saver, and the turn is
   streamed with `{"configurable": {"thread_id": ...}}` -- the framework
   appends the question to the thread's messages and the summarising
   middleware's state is in the checkpoint, so nothing is translated and
   nothing is handed in. **After a finished turn the adapter prunes the
   thread to its newest checkpoint**: without forks, nothing needs the
   history of a thread, and a conversation then costs one checkpoint between
   turns rather than one per step for ever. The final answer is read as it
   is today, off the `values` stream.

5. **Pydantic AI keeps its conversations in a table of histories.** There
   is nothing to delegate to: on `pydantic-ai-slim` 2.47.0, the locked
   version, `Agent.run` still takes `message_history` from the caller, the
   `conversation_id` it accepts is a correlation id resolved from the
   history or minted as a UUID7 under which nothing is stored, and
   `durable_exec` is Temporal, DBOS and Prefect. So the adapter does what
   the examples' `PydanticAILocalState` does, in Postgres: one row per
   conversation, `pydantic_ai_histories (conversation_id PRIMARY KEY,
   messages bytea, updated_at)`, read at `open` with
   `ModelMessagesTypeAdapter`, upserted on the `AgentRunResultEvent` before
   `Done` is yielded, after the history processor has kept it within the
   window as today.

6. **A turn that did not end leaves no memory, still.** Pydantic AI writes
   nothing for a turn that raised or was cancelled. LangGraph's checkpointer
   writes a checkpoint per step, so a cancelled loop leaves the thread at a
   step with nodes still pending and a tool call the vendor would refuse to
   see unanswered; the adapter therefore opens a turn **from the newest
   checkpoint with nothing pending** (`next` empty) and streams it with that
   `checkpoint_id`, which forks the thread from there and leaves the dead
   steps on a branch the next finished turn's prune removes. The contract
   suite holds both adapters to one promise: what a turn that did not end
   said is not in the memory the next turn runs with. The per-step
   checkpoints are nonetheless there while a turn runs, which is what a
   `waiting` run -- a person's approval before a tool runs -- will stand on
   when it comes; that day the promise changes for that one state and not
   before.

7. **The conversation is one line.** `Message.parent_id` goes, and with it
   the tree: `core/conversation_tree.py` (the leaves, the visible path, the
   path to a message, the parent for a regeneration, the turn start),
   `visible_path(extending=)`, `tree_of`, `tree_of_stored`, `check_tree`.
   What stays of its rules is the chain, checked on append: a conversation
   begins with a user message; a run's messages follow the question in the
   order the run produced them; a tool message answers exactly the calls of
   the answer before it. A conversation's messages are read oldest first by
   the order they already have (`created_at`, then id). **Edits and
   regenerations go**: `regenerate` and `begin_again` from `Turns`, the
   `{"regenerate": uuid}` body and `parent_id` from the turn request, the
   `onEdit` and `onReload` actions from the interface, the "soft deletion
   by the shape of the tree" and the lineage for analytics from the specs,
   and [ADR 0003](../adr/0003-single-visible-thread.md) is superseded by the
   ADR of this plan. The way back, later, is **rewind** rather than a fork
   -- cut the transcript and the memory at the same message, which the
   checkpointer makes cheap for one framework and a truncated list for the
   other -- and it is not in this plan.

8. **A conversation stays with its engine, by construction.** Its memory is
   in one engine's tables under its id, and the other engine's `open` finds
   nothing. An agent moved to another engine therefore reaches its
   conversations as a loss of memory, once, as today: the application sees
   a transcript with messages and a backend whose `exists()` is false, and
   begins from nothing with a line in the log. The old engine's rows stay
   until the conversation is deleted; nothing reads them.

9. **The transcript stays the platform's, and a turn is two writes in two
   stores.** The transcript is written from the stream as today -- an answer
   with its calls, one tool message per batch of results, the next answer
   -- and read by nobody but people, the exports and retention; the model is
   never fed from it, and it is what makes the stored conversation readable
   whatever the framework summarised away. A finished turn writes the memory
   first, inside the adapter, before `Done`, and then the transcript's last
   message with the run's ending, in the platform's one transaction. A crash
   between the two leaves the model one answer ahead of the transcript; the
   other order cannot happen. Accepted and said in the spec: the memory is
   the framework's, the transcript is the people's, and one write between
   them is the price of keeping each where it belongs.

10. **Names.** `Engine` is the port's name for what the composition root
    builds; `AgentBackend` is the examples' name for one conversation in one
    framework and is kept; the engine is still `langgraph` in the
    configuration, on every run and in the adapter's package name. The
    adapter's five events keep their names and `Done` loses `state`.

## The seams, by layer

- **Domain** (`domain/events.py`, `domain/conversation.py`,
  `domain/run.py`): `Done(text)`; `MAX_ENGINE_STATE_BYTES` goes;
  `Message` loses `parent_id` and the rules about it; `Run` is unchanged
  (the record never carried the state).
- **Core** (`core/conversation_tree.py` gone; `core/runs.py`,
  `core/conversation_format.py`): a small `check_sequence` over a
  conversation's messages replaces the tree's shape checks -- the chain
  rule, the tool message answering the calls before it; the format's
  written form loses `parent_id` and the version note says so;
  `check_backend_events` and `check_event_order` do not change.
- **Ports** (`ports/agents.py`, `ports/storage.py` new,
  `ports/conversations.py`): `Engine.open(conversation_id, agent, *,
  model) -> AgentBackend`, `AgentBackend.exists / stream / close`, with the
  docstring's promises rewritten (what crosses, how a turn ends, what
  `close` releases, what the memory is and where); the `Database` protocol;
  `ConversationStore` loses `engine_state`, the `engine_state=` of
  `end_run`, the regeneration half of `start_turn` and every `parent_id`.
- **Application** (`application/turns.py`, `application/conversations.py`):
  `execute` opens the backend for the run, streams the question's text,
  closes it in the `finally` of the same shield the ending is written under;
  `_memory`, `_path`, `regenerate`, `begin_again` and the parent in
  `_round` go, and a message is appended after the last one. `Conversations`
  loses the tree it builds on `open`; the resume point is the last message.
  The run lifecycle -- claim, submit, the pump, the ending under a shield,
  cancel, the sweep -- does not change.
- **Adapters** (`adapters/agents/langgraph/`: `engine.py`, `saver.py` new,
  `schema.sql` new; `adapters/agents/pydantic_ai/`: `engine.py`,
  `histories.py` new, `schema.sql` new): LangGraph -- the saver of decision
  4, `open` picks the newest clean checkpoint, `stream` runs the graph on
  the thread, `close` prunes after a finished turn; Pydantic AI -- the
  histories of decision 5, `open` reads, `stream` upserts before `Done`,
  `close` holds nothing. Both keep what is pinned today: the endpoint from
  the configuration, the key as an argument, the loggers held, tracing off,
  `CancelledError` through, nothing held after `close`. Both take a storage
  kind at construction: the `Database` for a deployment, memory for the
  tests. `read_state` / `write_state` go.
- **Datastore** (`datastore/schema.sql`, `datastore/schema.py`,
  `datastore/conversations.py`): `messages` loses `parent_id`, its composite
  foreign key and the comments about the tree; `runs` loses `engine_state`;
  `create_schema` applies the engines' files after the platform's, in the
  one transaction, and the pin is over the concatenation in a fixed order;
  `check_schema` lists the engines' tables among the ones that must be
  there; the store loses `engine_state` and the regeneration branch of
  `start_turn`.
- **Configuration and composition** (`app.py`, `cli.py`): the engines are
  built with the pool beside the models, the keys and the servers; `db
  init` and `version` know every schema file; the import contracts keep
  `asyncpg` under `datastore` and gain nothing, since the adapters import
  the protocol and not the driver; the architecture probes are unchanged in
  kind.
- **API, wire, frontend**: the turn request is `{"text": str}`; the
  conversation document has no `parent_id`; the `openapi.json` snapshot
  follows; the vendored thread's `onEdit` and `onReload` are not provided,
  which hides the actions, and the edit-refusal path of `runtime.tsx` goes
  with them.
- **Tests**: the contract suite (`contracts/agents.py`) is rewritten for the
  new port -- a streamed answer, an answer that was not streamed, a tool
  round, **the memory**: two turns on one conversation id and the scripted
  model shown the first on the second, two conversations that share
  nothing, a turn that raised or was cancelled not remembered, `exists`
  before and after, closing and holding nothing. `fakes/agents.py` keeps a
  dict of conversations. The adapters' own tests build them on the memory
  kind over their framework's scripted model, as today. New integration
  tests run each adapter's Postgres storage: the saver put, got, listed,
  pruned and cascaded; the histories upserted, read and cascaded. The tree
  tests, the regenerate tests of the lifecycle, the routes and the store
  contracts, and the interface's edit and reload tests go. Rough count: the
  tree module (422 lines) and the memory walk, the regenerate paths and the
  `engine_state` plumbing go; the saver (about 300 lines with the DDL) and
  the histories (about 80) come in.

## Order of work

Two parts, each one branch and one pull request, each step a commit that
leaves the unit suite green. The Postgres suite is run where a database is
to hand. The first part is a prerequisite of the second: the key of
decision 2 is one per conversation, which a forking transcript cannot use.

**Part 1 -- the line.**

1. **The plan**: this file and the progress file.
2. **Domain, core and the format**: `parent_id` out, the tree out,
   `check_sequence` in; the domain, core and format tests.
3. **Datastore and the store contracts**: the schema without `parent_id`
   and the regeneration, the pin, the fakes, the contract suite.
4. **Application, API, wire and interface**: `Turns` without `regenerate`
   and `begin_again`, `Conversations` without the tree, the turn request,
   the snapshot, the thread without edit and reload; their tests. ADR 0006
   written for this part, superseding ADR 0003.

**Part 2 -- the adapters' own memory.**

5. **The port and the fake**: `Engine`, `AgentBackend`, `Database`,
   `Done(text)`; the fake engine; the contract suite rewritten.
6. **The LangChain adapter on the memory kind**: `open`, `stream`, `close`
   over `InMemorySaver`; its tests on the scripted model; the contract
   suite green.
7. **The Pydantic AI adapter on the memory kind**: the same over a dict.
8. **The application and the datastore**: `execute` opens and closes a
   backend; `_memory` and `engine_state` out everywhere; `create_schema`
   over several files; the lifecycle and store tests.
9. **The database kinds**: the saver and its DDL, the histories and theirs;
   the integration tests; `db init`; the composition root; the live tests.
10. **The specs, ADR 0007, the checks** and the progress note with the line
    count.

## Spec sentences to change

- [specs/core.md](../specs/core.md): "Persistence" -- the transcript is the
  platform's, the memory is each framework's, in tables of its adapter's in
  the one database; the seams list names the adapters' storage.
- [specs/agents.md](../specs/agents.md): "The agent port" rewritten for
  `Engine` and `AgentBackend`; "A conversation stays with its engine" by
  construction; the discard test gains the adapter's schema file.
- [specs/conversations.md](../specs/conversations.md): "Shape" -- one line,
  no tree, no edits or regenerations for now; "The format" -- no
  `parent_id`; "The model's memory" -- the framework's, in its adapter's
  tables, deleted with the conversation; "Persistence" -- two writes, the
  memory first; "The visible thread" goes; "Forking" (planned) notes that a
  fork of a conversation needs the adapter to copy a memory.
- [specs/runs.md](../specs/runs.md): a run records no memory; "a run that
  did not end stored no memory" stays true and says how for each engine;
  "Details likely to change" loses the state-per-step remark, which is now
  the case.
- [specs/wire.md](../specs/wire.md): the turn request is a text; no
  `parent_id`, no `regenerate`.
- [specs/backend.md](../specs/backend.md): "The schema is entirely the
  platform's. No framework creates or migrates tables" becomes: the
  platform's schema file and each adapter's are applied together by the
  command, under one version and one pin; no framework applies anything on
  its own, and the server still refuses a database that does not match.
- [specs/privacy.md](../specs/privacy.md): deleting a conversation deletes
  its memory, by the cascade into the adapters' tables.
- [layout.md](../layout.md): the ports list, the adapters section (an
  adapter may own tables for its framework's state, through the `Database`
  protocol), the responsibilities table (the model's memory: the adapter,
  in its own tables), the enforcement list.
- [ADR 0003](../adr/0003-single-visible-thread.md): superseded by ADR 0006
  (one line of messages). [ADR 0005](../adr/0005-the-framework-owns-the-loop-and-the-memory.md):
  its memory decision superseded by ADR 0007 (the memory is the adapter's,
  in its tables); the rest stands.
- [DEPENDENCIES.md](../../DEPENDENCIES.md): the
  `langgraph-checkpoint-postgres` row says the saver is our own over
  `asyncpg`; no new row.
- [README.md](../../README.md): "Message editing and replaying" leaves
  "What it does today" until rewind comes; "Backing up Robinauts means
  backing up one database" stays true.

## Open

- **Rewind.** Editing a message or regenerating an answer as a cut of both
  the transcript and the memory at one message: LangGraph by the
  `checkpoint_id` recorded on the answer's provenance, Pydantic AI by
  truncating its list. Needs a `rewind(to)` on the port and the end of the
  prune of decision 4 for the checkpoints a rewind may need. Not in this
  plan.
- **Forking a conversation** (planned in the spec) needs the adapter to copy
  a memory to a new id: `copy(from, to)` on the port, one more method when
  the feature comes.
- **Approval before a tool runs** stands on decision 6's per-step
  checkpoints for LangGraph and on `DeferredToolRequests` for Pydantic AI,
  and on the `waiting` run state that is already in the machine. The
  promise of decision 6 changes for a `waiting` run then.
- **The examples' `StorageKind.POSTGRES`** is declared and not implemented.
  The saver and the histories of this plan are that implementation; moving
  them back into the examples, or the examples pointing here, keeps the
  reference honest.
- **The order of messages** stays `(created_at, id)`. A `seq` per
  conversation would be the honest order for a line and a smaller resume
  point; it is a datastore change of its own and is not needed by anything
  here.
- **Dead branches.** A cancelled turn's checkpoints stay until the next
  finished turn prunes them, or the conversation is deleted. Bounded by one
  turn's steps; a sweep by age is the remedy if it ever shows.
