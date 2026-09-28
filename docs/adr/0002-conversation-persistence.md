# ADR 0002 — Conversation persistence: the platform owns the record, agent frameworks are stateless per turn

- Status: accepted; superseded in part by
  [ADR 0004](0004-context-management-in-the-adapter.md) (the paragraph on
  what lives above the agent port, and its trimming example)
- Date: 2026-09-20

## Context

The core spec requires that the agent engine be a port with two swappable
implementations (LangGraph and Pydantic AI), and that "any conversation can
be continued with any framework and any vendor". Conversations, history and
memories stay in the one database of the deployment (goals 3 and 6).

The two frameworks treat persistence very differently:

- **LangGraph** persists natively through a *checkpointer*.
  `langgraph-checkpoint-postgres` provides `PostgresSaver` /
  `AsyncPostgresSaver`, which create and migrate their own tables and store,
  per `thread_id`, a snapshot of the whole graph state at every step, as
  serialized LangChain objects. This is **execution state**: it is what
  allows a run to resume after a crash, to pause on an interrupt and
  continue later, and to fork from an earlier step. The message history is
  in there because messages are part of the graph state; it is not designed
  as a conversation record to be read by anything other than LangGraph.
- **Pydantic AI** has no built-in persistence. An agent is stateless: the
  caller passes `message_history`, receives the new messages back, and
  stores them itself.

Since Pydantic AI stores nothing, the platform needs its own conversation
store in any case. The question is whether LangGraph's checkpointer should
also hold conversations — as the source of truth with an export, or in a
dual write alongside the platform's store.

### Licence conflict

`langgraph-checkpoint-postgres` (MIT itself, 3.1.2) has hard dependencies on
`psycopg>=3.2.0` and `psycopg-pool>=3.2.0`. `psycopg` is
**LGPL-3.0-only** (3.3.6). Checked on PyPI, 2026-09-20.

The project's dependency policy forbids LGPL, including transitively
(goal 1). **`langgraph-checkpoint-postgres` cannot be adopted as it is.**
The LangGraph core (`langgraph`, `langgraph-checkpoint`, `langchain-core`)
does not have this dependency and is not affected.

The same finding excludes `psycopg` as the platform's own Postgres driver;
the platform uses `asyncpg` ([specs/backend.md](../specs/backend.md)).

## Decision

**The platform owns the conversation record. Both agent adapters are
stateless per turn. No framework persistence is used.**

- Conversations are stored in the platform's own tables, in a format the
  platform owns — not the format of any framework or model vendor. This
  store is the only source of truth for conversations.
- The LangGraph adapter compiles its graph **without a checkpointer**. The
  Pydantic AI adapter passes `message_history`. Neither framework remembers
  anything between turns, so there is nothing to migrate when the framework
  or the vendor changes.
- Each adapter translates between the platform's message format and its
  framework's format, in both directions, on every turn.

### The turn lifecycle

The controller, which knows only the agent port, runs every turn the same
way:

1. Load the conversation's messages from the platform's database.
2. Call the agent port with the history and the new user message; stream
   the deltas to the UI.
3. Translate the new messages produced by the run into the platform's
   format and append them to the conversation. Record token usage per model
   and conversation (goal 7).
4. The next turn starts again from step 1 — with whichever adapter and
   vendor are configured at that moment.

Anything that must behave identically under both frameworks lives above the
agent port, not inside an adapter. Trimming or summarising a long history
to fit a context window is the first example.

> The paragraph above is superseded by
> [ADR 0004](0004-context-management-in-the-adapter.md) (2026-09-28): the
> application hands the port the full visible path and the tools, and each
> adapter owns its context policy. What stays above the port is the turn
> lifecycle and the record.

### What is not decided

Whether to use a framework's own persistence at all — for example a
LangGraph checkpointer so that a run can pause for human approval of a tool
call, or recover after a crash — is **not decided here and not planned**.
The first version has no tool usage and a turn is a single model call, so
nothing of that kind is needed. It will be evaluated if and when a feature
requires it.

How such persistence would sit beside the platform's store is part of that
evaluation and is open. (Tools came on 2026-09-28 and met the question:
the platform owns the tool loop and needs no framework persistence for it,
[ADR 0004](0004-context-management-in-the-adapter.md).) **Dual write** — LangGraph storing its checkpoints
its own way while the platform also writes its own record — is one of the
options to discuss then. It is neither adopted nor rejected by this ADR. Its
known difficulty is keeping the two in step: when a user edits, regenerates
or deletes a message in the platform's store, a checkpoint of the same
conversation is stale.

Two constraints already bind that evaluation:

- the platform's store stays complete and framework-neutral, so that any
  conversation can still be continued with any framework and any vendor;
- the licence conflict above: the stock Postgres checkpointer cannot be
  used as it is.

## Consequences

Good:

- The two adapters are symmetric, which is what makes them swappable at any
  time. A conversation started on one framework and vendor takes its next
  turn on another with no migration.
- The history list, usage reporting, export, retention and deletion all
  read one store with a known schema. Deleting a conversation deletes it;
  no copy survives in a framework's tables.
- The schema of the platform's database is entirely the platform's. No
  framework creates or migrates tables in it.
- No LGPL dependency.

Costs:

- The platform designs and maintains its own message format, and one
  translator per adapter. The format must be able to carry tool calls and
  tool results later without breaking (core spec, "Tool usage").
- LangGraph's native persistence features — resume, interrupts,
  time travel — are not available. In the first version nothing needs them.
- The full history is translated on every turn. This is negligible next to
  the model call, which receives the full history in any case.

## Alternatives considered

- **LangGraph's checkpoints as the source of truth, exporting when
  needed.** Rejected. Swapping "at any time" would become a migration;
  Pydantic AI cannot read checkpoints; the history list, usage reporting
  and deletion would depend on parsing blobs whose format belongs to a
  library. It also requires the dependency that the licence policy
  excludes.
