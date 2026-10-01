# ADR 0005 — The framework owns the loop, the context and the model's memory

- Status: accepted; supersedes
  [ADR 0004](0004-context-management-in-the-adapter.md) and the rest of the
  decision of [ADR 0002](0002-conversation-persistence.md) (the record stays
  the platform's; the frameworks are no longer stateless per turn, and a
  conversation no longer crosses engines)
- Date: 2026-09-30

## Context

The two agent frameworks were brought in for three things: the vendors'
model clients, context window management — trimming, pruning, summarising —
and the techniques that make a prompt cache pay. Robinauts was to carry
messages between the interface and an agent, and nothing more.

What was built by ADR 0002 and ADR 0004 paid the frameworks' cost and took
none of that. The platform owned the tool loop: an engine yielded a call and
stopped, the application called the tool over an MCP client of its own and
ran the engine again from the stored history. The platform owned the memory:
the conversation was translated from the platform's format into each
framework's format and back on every turn, the vendor's signed thinking
blocks carried in `extras` and replayed by hand. And each adapter's context
policy was "everything", because a real one needs the framework's middleware
over the framework's own history, which the adapter never had. Two adapters
of a thousand lines each, a hand-written MCP client of seven hundred and
fifty, a run lifecycle of two thousand four hundred, most of it the loop —
and neither framework doing what it was brought in for.

The contract to build against is the one
[agent-framework-examples](https://github.com/the-guish/agent-framework-examples)
reached on `feature/event-streaming`: an `AgentBackend` is one conversation
in one framework, whose framework keeps the state between turns, runs the
whole turn — tools included — and streams a handful of events in a form no
framework defines.

## Decision

**The agent adapter runs the whole turn on its framework — the loop, the
context, the memory — and the platform keeps a transcript.**

- The port is the examples' contract, made async: `stream(agent, prompt, *,
  model, state)` yields `TextDelta`, `ReasoningDelta`, `ToolCall`,
  `ToolResult` and ends with `Done`, which carries the final answer's text
  and the framework's **state** after the turn, as bytes.
- **The memory is the framework's own serialisation of its history, and the
  platform never reads it.** It is stored on the run (`runs.engine_state`),
  written with the run's ending in the same transaction, and read by the
  next turn from the nearest finished run of the same engine on the visible
  path above the question — so a fork of the transcript is a fork of the
  memory, and a turn that did not end leaves no memory. Not a framework
  checkpointer: one mechanism serves both frameworks, deleting a
  conversation deletes its memory by the cascade already there, no second
  database client is opened, and `langgraph-checkpoint-postgres` still
  brings `psycopg` (LGPL).
- **The transcript is the platform's**, in the format ADR 0002 gave it: the
  message tree, the visible thread, edits and regenerations, the tool calls
  and results, the exports. It is written from what the adapter streamed and
  the model is never fed from it. It exists for the people reading the
  conversation and for analysis.
- **A conversation stays with its engine.** A state written by another
  engine is not read; the turn begins from nothing with the transcript
  intact and a line in the log. Changing an agent's engine reaches its
  existing conversations as a loss of memory, once. The swap test — a
  conversation started on one engine continues on the other — is a claim
  the platform no longer makes, and the swap fixtures go with it.
- **Tools go through the frameworks' MCP clients**, which run the tool
  inside the loop: `langchain-mcp-adapters` and Pydantic AI's `MCPToolset`.
  The port over tool servers, the client of our own, the naming of tools
  above the port and the stub definitions for tools a history called are
  gone. The configuration keeps a server's table without its `prefix`: a
  tool's name is the framework's, `<server id>_<tool>` under both.
- **Context management and the cache are the frameworks'**: LangChain's
  summarisation and prompt-caching middleware, Pydantic AI's history
  processor and cache settings, each measuring against the model's
  `context_window` when configured, the framework's knowledge of the model
  otherwise, and a default last. The invariant of ADR 0004 holds by
  construction: neither ever cuts the turn it is answering.
- **The bound on tool rounds is each adapter's** — LangChain's recursion
  limit, Pydantic AI's request limit — and the turn's timeout and
  cancellation stay above the port.
- The dependencies come in now — `langchain`, `langchain-mcp-adapters`,
  `mcp`, `pydantic-ai-slim[mcp]`, `fastmcp` — and the licence decision on
  the MCP SDK's tree comes later, with the pull request. The gate is
  expected red until then. Taken (2026-09-30): `MIT-0` is allowed, and the
  lock is resolved for Linux and macOS only, which leaves `pywin32` out;
  Windows is not a target ([DEPENDENCIES.md](../../DEPENDENCIES.md)).

## Consequences

Good:

- The frameworks do what they were brought in for, with their own means:
  summarising, trimming, caching, running tools, replaying a vendor's signed
  blocks. What was reimplemented above the port is deleted, and the net is
  a smaller codebase with the same interface, wire and stored format.
- The application has one path for every turn: read the transcript, find
  the memory, stream the adapter, write down what it says.
- The seam is still agent-framework agnostic, and the two adapters are held
  to one contract suite that asks nothing about the framework, so a third
  framework is a third adapter.

Costs:

- **A conversation cannot move between engines.** Accepted: there was never
  an intention to swap engines in the middle of a conversation; the second
  engine exists to keep the seam honest.
- **The memory is opaque.** What a framework kept — a summary it wrote, a
  signed block, a tool result in its own spelling — cannot be inspected or
  migrated by the platform, and a framework that changes its serialisation
  between versions makes older memories unreadable, which the adapter
  reports as a failed turn rather than reading as nothing. The transcript
  is the durable record; the memory is the framework's cache of it.
- **A turn that did not end leaves no memory.** The model does not remember
  a cancelled or failed turn's calls and results, though the transcript
  shows them. A state per step of the loop is the remedy if it matters.
- The context policies are the frameworks', with the frameworks' knobs:
  what to keep and when to summarise is a number each adapter chooses, and
  reviewing it means reading the framework's documentation.

## Alternatives considered

- **Keep the platform's loop and add context management above the port.**
  Rejected: it is ADR 0004's alternative again, and the reason it was
  rejected there — re-implementing inside `core` what each framework
  exposes through its own API — is the reason the frameworks were not paying
  their way.
- **A framework checkpointer per engine**, as the examples' LangChain
  backend uses. Rejected: two persistence mechanisms for two frameworks,
  tables a framework migrates in the deployment's database (against
  [specs/legacy/backend.md](../specs/legacy/backend.md)), a second connection pool, and
  `psycopg` in the tree. A column of the run does the same for both.
- **Keep the transcript as the memory** — translate it into each framework's
  format on every turn, as before, but let the framework run the loop.
  Rejected: the translation is what the memory decision removes, and a
  summary the framework writes has nowhere to live in the transcript
  without becoming a message a person would read.
