# Exploration: the model's memory as a pointer, not a blob

Written 2026-10-01 on `claude/nifty-cray-fa9ooe`. The question: can the
platform stop storing the framework's state as bytes on the run
(`runs.engine_state`) and keep only an id that points at persistence the
agent adapter owns -- the way the caller in
[agent-framework-examples](https://github.com/the-guish/agent-framework-examples)
knows a conversation id and nothing else? Not a plan: what the examples
actually do, what the platform's own rules demand of such a design, three
shapes it could take, what each costs, and a recommendation. The decision it
would revisit is
[ADR 0005](../adr/0005-the-framework-owns-the-loop-and-the-memory.md), and
the note it would follow is [framework-loop-plan.md](framework-loop-plan.md),
decision 2.

## What the examples do

**The persistence model is in the shared base, not in either branch.**
`feature/event-streaming` (`66891a5`) and `feature/event-streaming-b`
(`d702815`) both branch from `c2b939d` ("persistence"), and that commit is
where the caller stops knowing the state:

- `common/conversation_store.py`: the caller's store keeps a
  `ConversationRecord(conversation_id, backend, model, context_window)` and
  nothing else. It is a pointer: which backend, on which model, under which
  id.
- `common/registry.py`: a backend is built by a factory handed
  `(store, conversation_id, tools, model, context_window)`. The store is
  passed so that the backend can find **its own** storage under the store's
  root (`<root>/backends/<name>/`), keyed by the conversation id.
- Each backend keeps its state its own way: LangChain, a SQLite checkpointer
  with `thread_id = conversation_id`; Pydantic AI, a JSON file of
  `ModelMessage`s per conversation, written by the backend itself
  (`pydantic_ai_local_state.py`); OpenAI Agents, a `SQLiteSession`;
  Strands, a `FileSessionManager`.
- `conversation_exists()` lets the runner refuse to create over a leftover
  and refuse to resume with nothing: the pointer and the state are two
  writes, and the runner checks they agree.
- `StorageKind.POSTGRES` is declared with `{"url": ...}` and not
  implemented. Read literally, it means each backend opens its own
  connection to the shared database.

**The session id is the conversation id, and there is one thread per
conversation.** The examples have no edits and no regenerations, so a
conversation is one line and the id of the line is enough.

**The two branches change the shape of a turn, not where the state lives.**

| | `feature/event-streaming` | `feature/event-streaming-b` |
|---|---|---|
| the turn | sync `stream(prompt) -> Iterator[Event]`; `ask` is `stream` with the events thrown away | `async ask(prompt, on_event) -> str`, a callback per event |
| the loop | each backend owns an asyncio loop for the life of the conversation (`common/aio.iterate`), because a framework's HTTP client binds itself to the loop it first ran on | one loop per process; everything on the port is `async`, including `conversation_exists`, `set_model`, the store's `load`/`save`, and the backend factories, which do their I/O (open the SQLite connection, load the JSON history) before the backend exists |
| the events | `TextDelta`, `ToolCall`, `ToolResult`, `Done(text)` | `TextDelta`, `ThinkingDelta`, `ToolCallStarted`, `ToolCallFinished(is_error)`; the answer is the return value |
| releasing | the generator's `finally` | `close()` on the backend, which the LangChain backend uses to close its `aiosqlite` connection |
| LangChain's answer | the last `AIMessage` seen in the `updates` stream | read back from the checkpointed thread (`aget_state`): the checkpointer is trusted as the memory |

For the question asked, the branches are therefore **one option, not two**.
What the platform already took is the generator shape of the first branch,
made async (`ports/agents.py`), with the second branch's thinking delta and
error flag already in `domain.events`. If anything of the second branch
applies here it is the posture that a backend's I/O is awaited before a
turn begins and released by an explicit close, which the platform does with
the memory lookup in the application and the generator's `aclose()`.

**One thing in the examples is a demo device, not a design.** `main.py`
keeps a backend object alive for the conversation inside a subprocess,
closes the process, and resumes in a new one to prove the follow-up works
from storage alone. A "session id pointing at the executing adapter" cannot
mean a live object here: a run outlives its request, a conversation's turns
are hours apart, and the executor is asyncio tasks in whichever process
took the run up ([specs/runs.md](../specs/runs.md), "Where the work
happens"; a `robinauts worker` is named as a later adapter of
`RunExecutor`). The pointer points at **storage**, and the storage must be
reachable from every process, which in this deployment means the one
database.

## What the platform does today, and why

- The port: `Agent.stream(agent, prompt, *, model, state: bytes | None)`
  yields events and ends with `Done(text, state: bytes | None)`, bounded by
  `MAX_ENGINE_STATE_BYTES` (64 MiB). One engine instance per deployment,
  shared by every conversation; the adapter holds nothing between turns and
  is handed the memory (`ports/agents.py`).
- The column: `runs.engine_state bytea`, written by
  `ConversationStore.end_run(..., engine_state=)` in the same transaction as
  the run's ending, read by `engine_state(run_id)`; `NULL` for a run that
  did not finish (`datastore/schema.sql`, `datastore/conversations.py`).
- The lookup: `Turns._memory` walks the visible path above the question,
  newest first, and takes the first finished run's state, if the same
  engine wrote it; otherwise the turn begins from nothing with a line in the
  log (`application/turns.py`).
- The serialisation: langchain-core's `messages_to_dict` of the graph's
  final `messages`; Pydantic AI's `ModelMessagesTypeAdapter` of
  `result.all_messages()` after the history processor. Neither engine uses
  a framework checkpointer: `create_agent` is built per turn without one,
  and Pydantic AI has none to use.

The reasons are recorded in ADR 0005 and
[framework-loop-plan.md](framework-loop-plan.md), decision 2, and every one
of them is a constraint on what a pointer design must give back:

1. one mechanism serves both frameworks;
2. **a fork of the transcript is a fork of the memory for free**, because
   the memory is per run and found along the path;
3. deleting a conversation deletes its memory by the cascade already there;
4. no second database client and no second connection pool;
5. no framework creates or migrates a table in the deployment's schema
   ([specs/backend.md](../specs/backend.md));
6. `langgraph-checkpoint-postgres` brings `psycopg`, which is LGPL and
   forbidden ([DEPENDENCIES.md](../../DEPENDENCIES.md)).

And the costs ADR 0005 accepted, which a pointer design could pay back: a
turn that did not end leaves no memory, so a cancelled or failed tool loop
is forgotten by the model though the transcript shows it; "a state per step
of the loop is the remedy if it matters".

## What "the caller knows only a session id" has to answer here

Five things the blob on the run gets for free, and a pointer has to earn.

**1. Forks.** The examples' session id is the conversation id because they
never fork. Here every edit and every regeneration forks: the next turn must
resume from the memory *as of the run before the one being replaced*
([specs/conversations.md](../specs/conversations.md), "Persistence";
[ADR 0003](../adr/0003-single-visible-thread.md)). A session per
conversation would carry the discarded turns into the model's memory. So the
pointer is **per run** -- "the memory as this run left it" -- or per branch
with a fork operation on the port, which is a second thing to get right.
Per run keeps `_memory` exactly as it is and changes only what it hands
over.

**2. Atomicity.** Today the memory and the ending are one write. With
adapter-owned persistence the adapter commits its session before yielding
`Done`, and the application then commits the ending with the pointer. In
between, a cancellation or a crash leaves a session nothing points at. That
is harmless to correctness as long as the pointer is only ever written with
a finished run, and it costs a sweep of orphans (by run, or by age). The
other order -- pointer without session -- cannot happen if the adapter
writes first and the application writes what `Done` carried.

**3. Deletion, retention, purge.** Today `ON DELETE CASCADE` from
`conversations` through `runs`. With a pointer, either the adapter's storage
is keyed by the conversation id as well and sits in the platform's schema
where the cascade reaches it, or the port grows `forget(conversation_id)`
and deletion becomes two stores to keep in step
([specs/privacy.md](../specs/privacy.md): a purge removes everything of a
person; a retention period deletes automatically).

**4. Several processes.** Said above: the pointer must resolve from any
process, so the session lives in the database. An in-process adapter object
per conversation is the examples' `IN_MEMORY` kind, which the platform
cannot have.

**5. Licence and layout.** `psycopg` is out. `asyncpg` is confined to
`datastore` by an import-linter contract ("asyncpg only under datastore");
each framework is confined to its adapter sub-package; no SQL outside
`datastore`; the schema is the platform's. So a LangGraph checkpointer that
talks to Postgres can be written neither in the adapter (no `asyncpg`) nor
in `datastore` (no `langgraph`). The only way through is a **port**: the
datastore implements an opaque store over the pool it already has, and an
adapter writes its persistence -- a checkpointer or a plain blob -- over
that port. That is the platform's version of the examples' "the backend
keeps its own data under the store's root": the root is a table.

## Three shapes

### Shape 1: the same blob, one table over

- A new port, say `EngineMemory`: `write(engine, conversation_id, key,
  data)`, `read(engine, key) -> bytes | None`, and
  `delete_conversation(conversation_id)` only if the cascade does not cover
  it. The datastore implements it over the existing pool, in a table
  `engine_memory (engine, conversation_id REFERENCES conversations ON
  DELETE CASCADE, key, data bytea, written_at)`. The composition root hands
  it to each engine at construction.
- The port changes type: `stream(agent, prompt, *, model, memory: str |
  None)` and `Done(text, memory: str | None)`. `runs.engine_state bytea`
  becomes `runs.memory_key text`. `_memory` is unchanged in shape.
- Each engine does what it does now and writes the bytes under a fresh key
  before yielding `Done`; the application stores the key with the ending.

What it buys: on its own, nothing. The bytes are still in the platform's
database, one table over; the run row is lighter, and the platform gains a
write path inside the turn and a sweep of orphaned keys. Done for its own
sake it is a refactor with a migration and no behaviour. It matters as the
step that lets an engine write **more than once per turn**, which is shape 3.

### Shape 2: each framework's own persistence, as the examples do

- **LangGraph.** A `BaseCheckpointSaver` -- `aget_tuple`, `alist`, `aput`,
  `aput_writes`, `adelete_thread`, over `CheckpointTuple(config, checkpoint,
  metadata, parent_config, pending_writes)` (checked against
  `langgraph-checkpoint` 4.2.0, the locked version). Over Postgres through
  `psycopg`: forbidden. Over `aiosqlite` (`langgraph-checkpoint-sqlite`,
  both MIT): a second database beside the one the deployment backs up,
  against the deployment goal of one database. Over `asyncpg`: impossible
  where the contracts stand. So it would be a saver written in the adapter
  over the port of shape 1, with the port widened to what a checkpointer
  needs -- a keyed, listable store of `(thread_id, checkpoint_ns,
  checkpoint_id) -> checkpoint, metadata, parent, pending writes`. The
  thread is the conversation, and the **pointer is the checkpoint id the
  turn ended at**: invoking the graph with a `checkpoint_id` in its config
  continues from that checkpoint and writes a new branch of the thread,
  which is LangGraph's time travel and exactly what an edit needs. That is
  the one real gain: a checkpoint per step of the loop, interrupts, and a
  turn that is resumable after a crash.
- **Pydantic AI.** There is nothing to delegate to. Checked on
  `pydantic-ai-slim` 2.47.0, the locked version: `Agent.run` still takes
  `message_history` from the caller; the `conversation_id` it now accepts is
  a correlation id resolved from the history or minted as a UUID7, and
  nothing is stored under it; `durable_exec` is Temporal, DBOS and Prefect,
  which make a *run* durable and keep no conversation;
  `DeferredToolRequests` / `DeferredToolResults` exist for approval flows,
  and the caller persists the history between the two runs. The examples'
  own Pydantic AI backend writes a JSON file for the same reason. So this
  adapter would write its `ModelMessagesTypeAdapter` bytes under a key --
  shape 1 again, under another name.

So shape 2 is two mechanisms, the thing ADR 0005 declined, with one
framework getting checkpoints and interrupts and the other a file in a
table. The asymmetry is permanent, and the contract suite -- which "asks
nothing about the framework" -- would have to hold the two to one promise
about a memory that one of them keeps per step and the other per turn.

### Shape 3: a pointer per run, one opaque store, written as the turn goes

Shape 1's port and table, with one allowance: an engine may write several
keys during a turn. `Done.memory` carries the key of the memory as the turn
ended, and the run records it; the engine may also report a key mid-turn --
an event, or a `latest(run)` the application can read -- so that a run that
was cancelled or failed can record the memory as of its last completed
step, which today the spec says plainly a run cannot do
([specs/runs.md](../specs/runs.md), "Details likely to change").

What stays: the fork rule in `_memory`; deletion by cascade; one pool and
no second client; no `psycopg`; the frameworks confined to their adapters;
"a conversation stays with its engine", since the store is keyed by engine
and a key written by another engine is not read. What changes: the engine
writes during the turn, through the port; the application stores a key, not
bytes; a sweep removes keys no finished run points at, with an allowance
for the key an unfinished run left.

What it enables: the features the specs name as planned and deferred for
want of exactly this. "Tools execution approval" ([README](../../README.md))
and "tools that suspend a run" ([specs/core.md](../specs/core.md),
"Planned") both need a memory as of the middle of a turn: LangGraph's
`interrupt` needs a checkpointer; Pydantic AI's deferred tools need the
caller to persist the history and resume with `deferred_tool_results`. The
`waiting` run state is already in the state machine for it
(`core/runs.py`), unreachable since the loop became the framework's. A
LangGraph checkpointer can then be laid over the same table when that work
comes, as shape 2 describes, without moving the Pydantic AI adapter at all.

## What shape 3 costs

- **The port.** `ports/agents.py` (`memory` in, `Done.memory` out),
  `domain/events.py` (`Done.state` becomes a key; `MAX_ENGINE_STATE_BYTES`
  moves to the store's contract). `core.check_backend_events` is unchanged.
- **A new port with a contract suite and a fake.** `ports/engine_memory.py`,
  its Postgres implementation in `datastore`, the table in `schema.sql` with
  the SHA re-pinned, `tests/contracts/engine_memory.py` run against both,
  and the fake handed to `ScriptedAgent`, which today records the bytes it
  was given (`tests/fakes/agents.py`, `Asked.state`) and would record the
  key and write a dict.
- **The conversation store.** `end_run(..., engine_state=)` and
  `engine_state(run_id)` become a key column: `ports/conversations.py`,
  `datastore/conversations.py`, `tests/fakes/conversations.py`,
  `tests/contracts/conversation_runs.py`.
- **The application.** `_round` returns a key; `_Ending.engine_state`
  follows; `_memory` hands over a key. A sweep of orphaned keys, which is new
  housekeeping and the first of its kind (the periodic sweep of runs is
  still "the several-processes work").
- **The engines.** Each gains the store at construction and a write before
  `Done` (and, for per-step memory, a write per model response): on the
  order of twenty lines each, plus the composition root handing the store
  in. `read_state` / `write_state` stay.
- **Tests.** `engine_state` is named in eight source and test files;
  `test_turn_lifecycle.py` alone names it nineteen times and scripts twenty
  `Done(... state=)`. The engine test modules script eight each.
- **Docs.** An ADR 0006 superseding the "a column of the run" paragraph of
  ADR 0005; [specs/agents.md](../specs/agents.md) ("The agent port"),
  [specs/conversations.md](../specs/conversations.md) ("The model's
  memory", "Persistence"), [specs/runs.md](../specs/runs.md),
  [specs/backend.md](../specs/backend.md), [layout.md](../layout.md)'s
  responsibility table; the `DEPENDENCIES.md` row for
  `langgraph-checkpoint-postgres` stands.

Two things no shape fixes, worth knowing before choosing one:

- **Every finished run stores the whole history again.** The blob today,
  a key per run tomorrow, and a LangGraph checkpoint too (a checkpoint
  carries every channel's value): storage over a conversation's life grows
  with the square of its length until the framework summarises. Old
  memories cannot simply be dropped, because an edit under an old answer
  resumes from that answer's run. A retention rule for memories -- keep the
  newest finished run's on each leaf path, let an edit of anything older
  begin from nothing -- is the same decision under every shape.
- **The memory stays opaque.** A pointer makes it more so: what is at the
  key is the framework's, and a framework that changes its serialisation
  still makes older memories unreadable, reported as a failed turn.

## Recommendation

- **Not shape 1 on its own.** The examples' caller knows only an id because
  each backend has a filesystem of its own; the platform's equivalent of
  that filesystem is a table in its own schema either way. The bytes would
  leave the `runs` row and not the database, at the price of a migration, a
  write path inside the turn and a sweep.
- **Shape 3 when a feature needs a memory mid-turn** -- tool approval, or a
  long tool loop worth recovering after a cancellation -- and shaped so
  that a LangGraph checkpointer can be laid over the same table then. Until
  then ADR 0005's column does the same job in one write.
- **Whatever the shape, the pointer is per run, never per conversation.**
  Forks are the one thing the examples do not have and the platform does;
  a per-conversation session is the one design that breaks them.
- **Of the two example branches, take the second's posture, not its
  shape.** Its all-async port with I/O awaited before the turn and an
  explicit release is what the platform already has; its callback-per-event
  `ask` is not an improvement over the generator the application closes to
  cancel.
