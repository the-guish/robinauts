# ADR 0005 — The framework owns the loop, the context and the model's memory

- Status: accepted. It supersedes
  [ADR 0002](0002-conversation-persistence.md) and
  [ADR 0004](0004-context-management-in-the-adapter.md). The record stays
  the platform's. The frameworks are no longer stateless per turn, and a
  conversation no longer crosses engines.
- Date: 2026-09-30
- Terminology (2026-11-06): what this record calls the model's memory, the
  code and the specs now call the engine's or the session's **state**, kept
  in each engine's `SessionStore`. "Memory" is kept for what a person keeps
  across conversations ([specs/core.md](../specs/core.md)).

## Context

We brought the two agent frameworks in for three things: the vendors' model
clients, context window management (trimming, pruning, summarising), and the
techniques that make a prompt cache pay. Robinauts was to carry messages
between the interface and an agent, and nothing more.

ADR 0002 and ADR 0004 paid the frameworks' cost and took none of that.

- **The platform owned the tool loop.** An engine yielded a call and
  stopped. The application called the tool over an MCP client of its own,
  then ran the engine again from the stored history.
- **The platform owned the memory.** Each turn translated the conversation
  from the platform's format into each framework's, and back. The vendor's
  signed thinking blocks travelled in `extras`, replayed by hand.
- **Each adapter's context policy was "everything".** A real policy needs
  the framework's middleware over the framework's own history, and the
  adapter never had that history.

The result was two adapters of a thousand lines each, a hand-written MCP
client of seven hundred and fifty, and a run lifecycle of two thousand four
hundred, most of it the loop. Neither framework did what it came for.

The contract to build against is the one
[agent-framework-examples](https://github.com/the-guish/agent-framework-examples)
reached on `feature/event-streaming`. There, an `AgentBackend` is one
conversation in one framework. Its framework keeps the state between turns,
runs the whole turn, tools included, and streams a handful of events in a
form no framework defines.

## Decision

**The agent adapter runs the whole turn on its framework: the loop, the
context and the memory. The platform keeps a transcript.**

- **The port is the examples' contract, made async.** `stream(agent, prompt,
  *, model, state)` yields `TextDelta`, `ReasoningDelta`, `ToolCall` and
  `ToolResult`, and ends with `Done`. `Done` carries the final answer's text
  and the framework's **state** after the turn, as bytes.
- **The memory is the framework's own serialisation of its history.** The
  platform never reads it.
  - It is stored on the run (`runs.engine_state`), written with the run's
    ending in the same transaction.
  - The next turn reads it from the nearest finished run of the same engine
    on the visible path above the question. A fork of the transcript is
    therefore a fork of the memory, and a turn that did not end leaves no
    memory.
  - No framework checkpointer is used. One mechanism serves both
    frameworks. Deleting a conversation deletes its memory through the
    existing cascade. No second database client opens. And
    `langgraph-checkpoint-postgres` still brings `psycopg` (LGPL).
- **The transcript is the platform's**, in the format ADR 0002 gave it: the
  message tree, the visible thread, edits and regenerations, the tool calls
  and results, and the exports. It is written from what the adapter
  streamed. The model is never fed from it. It exists for the people reading
  the conversation, and for analysis.
- **A conversation stays with its engine.** An adapter ignores a state
  another engine wrote. The turn then begins from nothing, with the
  transcript intact and a line in the log. Changing an agent's engine reaches
  its existing conversations as a loss of memory, once. The platform no longer
  claims that a conversation started on one engine continues on the other.
  The swap test and its fixtures go.
- **Tools go through the frameworks' MCP clients**, which run each tool
  inside the loop: `langchain-mcp-adapters` and Pydantic AI's `MCPToolset`.
  These go: the port over tool servers, our own client, the naming of tools
  above the port, and the stub definitions for tools a history called. The
  configuration keeps a server's table without its `prefix`. A tool's name
  is the framework's, `<server id>_<tool>` under both.
- **Context management and the cache are the frameworks'.** LangChain uses
  its summarisation and prompt caching middleware. Pydantic AI uses its
  history processor and cache settings. Each measures against the model's
  `context_window` when it is configured, then against the framework's
  knowledge of the model, then against a default. ADR 0004's invariant holds
  by construction: neither framework ever cuts the turn it is answering.
- **Each adapter bounds its own tool rounds**: LangChain with its recursion
  limit, Pydantic AI with its request limit. The turn's timeout and
  cancellation stay above the port.
- **The dependencies come in now**: `langchain`, `langchain-mcp-adapters`,
  `mcp`, `pydantic-ai-slim[mcp]` and `fastmcp`. The licence decision on the
  MCP SDK's tree comes later, with the pull request, and the gate is
  expected red until then. Taken on 2026-09-30: `MIT-0` is allowed. The lock
  covers Linux and macOS only, which leaves `pywin32` out. Windows is not a
  target ([DEPENDENCIES.md](../../DEPENDENCIES.md)).

## Consequences

Good:

- The frameworks do what they came for, with their own means: summarising,
  trimming, caching, running tools and replaying a vendor's signed blocks.
  What we had rebuilt above the port is deleted. The codebase shrinks, with
  the same interface, wire and stored format.
- The application has one path for every turn: read the transcript, find
  the memory, stream the adapter, write down what it says.
- The seam stays independent of any framework. One contract suite holds both
  adapters and asks nothing about the framework. A third framework is a
  third adapter.

Costs:

- **A conversation cannot move between engines.** We accept that. Nobody
  meant to swap engines in the middle of a conversation. The second engine
  exists to keep the seam honest.
- **The memory is opaque.** The platform cannot inspect or migrate what a
  framework kept: a summary it wrote, a signed block, a tool result in its
  own spelling. A framework that changes its serialisation between versions
  makes older memories unreadable. The adapter reports that as a failed
  turn rather than reading it as nothing. The transcript is the durable
  record. The memory is the framework's cache of it.
- **A turn that did not end leaves no memory.** The model forgets a
  cancelled or failed turn's calls and results, though the transcript shows
  them. A state per step of the loop would remedy that, if it matters.
- **The context policies are the frameworks'**, with the frameworks' knobs.
  Each adapter picks the numbers for what to keep and when to summarise.
  Reviewing them means reading the framework's documentation.

## Alternatives considered

- **Keep the platform's loop and add context management above the port.**
  We rejected it. It is ADR 0004's rejected alternative again. That
  rejection's reason, rebuilding in `core` what each framework exposes
  through its own API, is why the frameworks were not paying their way.
- **A framework checkpointer per engine**, as the examples' LangChain
  backend uses. We rejected it. It means two persistence mechanisms for two
  frameworks, and tables a framework migrates in the deployment's database,
  against [specs/core.md](../specs/core.md). It also means a second
  connection pool, and `psycopg` in the tree. A column of the run does the
  same for both.
- **Keep the transcript as the memory**, translating it into each
  framework's format on every turn as before, but let the framework run the
  loop. We rejected it. The memory decision removes exactly that
  translation. A summary the framework writes would also have nowhere to
  live in the transcript without becoming a message a person reads.
