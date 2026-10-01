# Plan: the frameworks own the loop, the context and the model's memory

Written 2026-09-30 on `claude/modest-newton-69yxl6`, after the tools over MCP
landed ([mcp-plan.md](mcp-plan.md), [mcp-progress.md](mcp-progress.md)).
Progress against it is in
[framework-loop-progress.md](framework-loop-progress.md). Not a spec: the
decisions, what they cost across the codebase, and the order to build it in.
The specs are rewritten with it -- the user's word is that only
[specs/core.md](../specs/core.md) is worth keeping, and even that changes --
and the spec sentences that change are listed at the end.

## Why

The two agent frameworks were brought in for three things: the vendors' model
clients, context window management (trimming, pruning, summarising) and the
techniques that make a prompt cache pay. Robinauts was to carry messages
between the interface and an agent, and nothing more.

What was built instead pays the frameworks' cost and takes none of that: the
platform owns the tool loop (an engine yields a call and stops, the
application calls the tool over a client of its own and runs the engine
again), the platform owns the memory (the conversation is translated from the
platform's format into each framework's format and back on every turn, with
the vendor's signed thinking blocks replayed by hand), and each adapter's
"context policy" is everything, because a real one needs the framework's
middleware over the framework's own history. Two adapters of a thousand lines
each, a hand-written MCP client of seven hundred and fifty, and a run
lifecycle of two thousand four hundred, most of it the loop.

The contract to build against is the one
[agent-framework-examples](https://github.com/the-guish/agent-framework-examples)
reached on `feature/event-streaming`: an `AgentBackend` is **one conversation
in one framework**, whose framework keeps the state between turns, runs the
whole turn -- tools included -- and streams four events in a form no framework
defines: `TextDelta`, `ToolCall` once its arguments are known, `ToolResult`
once it is back, and `Done` with the final answer. The platform then holds a
**transcript** for the people reading the conversation and for analysis, and
the model's memory is the framework's.

## What is wanted

- The agent adapters run the agent loop: the model asks for a tool, the
  framework calls it, the result goes back, and the model answers -- as many
  times as the turn needs -- and the platform sees it happen as events.
- The adapters own context management and prompt caching with the
  frameworks' own means: LangChain's middleware, Pydantic AI's capabilities
  and model settings.
- Each conversation's memory is kept **in its framework's format**, stored
  by the platform as opaque data, and never translated.
- The platform keeps a transcript in its own format -- the tree of messages
  it has today, tool calls and results included -- for the interface, the
  exports and analytics. It is written from what the adapter streamed; the
  model is never fed from it.
- Net lines deleted: the loop, the translators, the MCP client and their
  tests go; what comes in is a small port, a small event set and two
  adapters that are mostly the frameworks' own calls.

## Settled

Ten decisions, made when this plan was written. Where a spec or the code
still says otherwise, this is the direction and the spec changes.

1. **The port is the examples' `AgentBackend`, made async and stateless per
   call.** `ports.agents.Agent.stream(agent, prompt, *, model, state)`
   yields `domain.events.Event`s -- `TextDelta`, `ReasoningDelta`,
   `ToolCall`, `ToolResult`, `Done` -- and ends with `Done`, which carries the
   final answer's text and the framework's **state** after the turn, as
   bytes. `state` in is what the last finished turn of the conversation
   handed back, or `None` for a conversation with no memory yet. What the
   examples keep on a long-lived backend object (`conversation_exists`,
   `set_model`) the platform keeps on its records: a conversation's model is
   the conversation's and each run copies it, and "exists" is "there is a
   state". `ReasoningDelta` is the one event the examples do not have: their
   README names thinking as the next thing a chat interface needs, this
   interface already shows it, and both frameworks stream it. An adapter is
   one object per engine, shared by every conversation, as today.

2. **The state is the framework's own serialisation of its history, and the
   platform never reads it.** LangChain: the graph's final `messages`,
   written with langchain-core's own `messages_to_dict` / `messages_from_dict`.
   Pydantic AI: `result.all_messages()`, written with
   `ModelMessagesTypeAdapter`. What that carries is whatever the framework
   keeps -- the vendor's signed thinking blocks, a summary the middleware
   wrote, tool calls and results as the framework spells them -- with no
   translation and no rule of the platform's about it. It is stored on the
   **run** (`runs.engine_state`, `bytea`), written with the run's ending in
   the same transaction and read by the next turn. *Not* a framework
   checkpointer with tables of its own in the deployment's database: the
   examples' LangChain backend uses one, but here one mechanism serves both
   frameworks, a fork of the transcript (below) is a fork of the memory for
   free, deleting a conversation deletes its memory by the cascade that is
   already there, no second database client and no second connection pool
   is opened, and `langgraph-checkpoint-postgres` still brings `psycopg`
   (LGPL), which is the user's decision to take later and not this plan's to
   take in passing.

3. **The transcript is the platform's, and it forks as it does today.** The
   message tree, the visible thread, edits and regenerations stay exactly
   as [specs/legacy/conversations.md](../specs/legacy/conversations.md) has them: the
   application turns the adapter's events into the platform's messages
   (an answer with its tool calls, one tool message per batch of results
   under it, the next answer under that) and its own numbered run events,
   so the wire, the watcher, re-attaching and the interface do not change.
   What changes is what the model is fed: a turn resumes from the state of
   the **nearest finished run on its path** -- for a question, the run that
   produced the answer it hangs under; for a regeneration or an edit, the
   run before the turn being replaced -- so a fork of the transcript is a
   fork of the memory. **A turn that did not end leaves no state**: a
   cancelled or failed run wrote its calls and results into the transcript
   and nothing into the memory, so the model does not remember it. Said in
   the spec as what it is; the remedy, if it ever matters, is a state event
   per step of the loop rather than one at the end.

4. **A conversation stays with its engine.** Its memory is one framework's
   and the other cannot read it. The run records which engine wrote a state
   (`runs.engine`, which it already has); a state written by another engine
   is not read, and the turn begins from nothing with the transcript intact
   and a line in the log. So changing an agent's engine in the configuration
   reaches its existing conversations as a **loss of memory**, once, which
   the spec says plainly. The swap tests and the shared swap fixtures go:
   the claim they made -- a conversation started on one engine continues on
   the other -- is not one the platform makes any more. The discard test
   shrinks to the import in the composition root, the import contracts and
   the sub-package's own tests.

5. **Tools go through the frameworks' MCP clients.** LangChain:
   `langchain-mcp-adapters` (`MultiServerMCPClient` over Streamable HTTP,
   the credential as a header). Pydantic AI: the `MCP` capability
   (`pydantic-ai-slim[mcp]`, which brings `fastmcp`). Both run the tool
   inside the loop. What goes: the `ToolServers` port, the MCP client of our
   own over `httpx` (`adapters/tools/mcp/`), the naming of tools above the
   port (`core/tools.py`: prefixes, sorting, the `__` join, the 64-character
   rule), the `prefix` key of a server's table, `ToolDefinition`,
   `ListedTool`, `ToolResult`, the stub definitions for tools a history
   called (`tools_for_request`), the sentence for a call no tool message
   answers (`NO_RESULT`, `unanswered_calls`) and the fetch-once-per-run rule.
   What stays: a server's table (`url`, `auth`, `secret_env`, `user`,
   `timeout_seconds`), the secrets read at start-up and printed nowhere, an
   agent's `tools` line, the two tool parts and the `tool` role of the
   format, the run's tool events, the wire's four tool events, and the
   interface. A tool's name on the transcript is the name the framework used
   for it, whatever that is.

6. **Context management and caching are the frameworks'.** LangChain:
   `SummarizationMiddleware` (summarise the older history, keep the recent
   messages; the change is saved in the state, which is what the run stores)
   and `AnthropicPromptCachingMiddleware`. Pydantic AI: the
   `anthropic_cache*` model settings for the cache, and a `ProcessHistory`
   capability that keeps the history under a token budget. Both read one
   optional number of the model's configuration, `context_window` (tokens),
   for the trigger, and fall back to the framework's knowledge of the model
   where it is left out -- which is what the examples pass as
   `context_window` and for the same reason: a gateway's model ids are not in
   any framework's table. ADR 0004's one invariant -- the question being
   answered is whole in what the model sees -- is the frameworks' too: they
   never summarise the turn they are answering.

7. **The bound on tool rounds is each adapter's.** `max_tool_rounds` leaves
   `Turns` and the composition root; LangChain's `recursion_limit` and
   Pydantic AI's `UsageLimits(request_limit=)` bound the loop at the
   adapters' own default, and a turn that reaches it fails saying what the
   framework said. The turn's timeout stays above the port, over the whole
   turn, and so does cancellation: the adapter's generator is closed and lets
   the framework's run go.

8. **The dependencies come in now, the licence decision comes later.**
   `langchain` (for `create_agent` and the middleware),
   `langchain-mcp-adapters` and `mcp`, `pydantic-ai-slim[mcp]` and
   `fastmcp`. The licence gate was known to refuse the MCP SDK's tree
   (`cffi` states `MIT-0`, `pywin32` a family) and will; `DEPENDENCIES.md`
   says so in "Known exclusions", the user decides, and the pull request
   says it in so many words. Nothing else is decided about licences here.

9. **Names.** The engine is still `langgraph` in the configuration, on every
   run and in the adapter's package name: it is the operator's vocabulary and
   the datastore's `CHECK`, and the adapter is on LangChain's `create_agent`,
   which is a LangGraph graph. The platform's own published events are
   renamed where they clashed with the examples' event names: `TextDelta`
   becomes `TextPiece`, `ReasoningDelta` becomes `ReasoningPiece` and
   `ArgumentsDelta` becomes `ArgumentsPiece` -- the pieces of a message as the
   platform publishes them, which is the spec's own word -- and their written
   form (`text_delta`, `reasoning_delta`, `arguments_delta`) does not change,
   so nothing stored changes. The adapter's events keep the examples' names.

10. **What was published is what is stored, still.** An answer's text on the
    transcript is what the adapter streamed, joined; `Done.text` is the
    answer only when nothing was streamed (a provider that does not stream).
    A call's arguments are published once, whole, as the JSON the framework
    handed over, so the wire's `TOOL_CALL_ARGS` carries them in one piece and
    the stored part parses back to them. A tool message completes when it
    holds a result for every call of the answer before it; an answer
    completes at the first result that follows it, or at `Done`. Reasoning
    is published and kept in the run's events alone, as today.

## The seams, by layer

- **Domain** (`domain/events.py`, new; `domain/turn.py`, `domain/tools.py`,
  `domain/conversation.py`, `domain/agents.py`): the five events, with
  `Done(text, state)` bounded (`MAX_ENGINE_STATE_BYTES`); the eight engine
  events of `turn.py` go (`AnswerStarted`, `AnswerTextDelta`,
  `AnswerReasoningDelta`, `ToolCallStarted`, `ToolCallArgumentsDelta`,
  `ToolCallCompleted`, `AnswerCompleted`, `WaitingOnTools`) and the three
  renamed published events stay; `tools.py` keeps the spelling of a call's
  id and of a tool's name and loses the definitions, the results, the prefix
  rules and `NO_RESULT`; `conversation.py` loses `tools_for_request`,
  `unanswered_calls` and `NO_LONGER_OFFERED`; `ToolServerConfig` loses
  `prefix`; `ModelConfig` gains `context_window`.
- **Core** (`core/runs.py`, `core/tools.py`, `core/models_config.py`):
  `check_engine_events` becomes `check_backend_events` over the five events
  (text and reasoning before a `Done`; a result answers a call announced
  before it, once; `Done` last and once; at least one answer); `core/tools.py`
  goes; the parser reads `context_window` and no longer reads `prefix`.
  `check_event_order` does not change.
- **Ports** (`ports/agents.py`, `ports/tool_servers.py`, `ports/conversations.py`):
  `Agent.stream(agent, prompt, *, model, state)`; `ToolServers` goes;
  `ConversationStore.end_run(..., engine_state=)` and
  `engine_state(run_id) -> bytes | None`.
- **Application** (`application/turns.py`): one round per turn. `_produce`
  reads the tree once, takes the path to the question, finds the state at
  the nearest finished run of the same engine on the path above it, streams
  the adapter with the question's text, and turns the events into messages
  and run events: a `TextDelta`, `ReasoningDelta` or `ToolCall` with no
  answer open opens one; a `ToolResult` completes the open answer with its
  calls and opens the tool message under it; the tool message completes
  when every call has its result; `Done` completes what is open and ends the
  turn, and the state goes into the store with the run's ending. The tool
  loop, `_tools_for`, `_answered`, `_called`, `_Round`, the rounds bound and
  its two failures, `NO_SUCH_TOOL`, `NO_CONTENT`, `tool_servers` and
  `servers` go.
- **Adapters** (`adapters/agents/langgraph/engine.py`,
  `adapters/agents/pydantic_ai/engine.py`, `adapters/tools/mcp/` gone):
  LangChain -- `create_agent(model, tools, system_prompt, middleware)` over
  the tools `langchain-mcp-adapters` lists for the agent's servers, the
  history from the state plus the question, `astream` with `messages`,
  `updates` and `values`; text and reasoning off the chunks, calls and
  results off the node updates, the final state off the last values.
  Pydantic AI -- `Agent(model, instructions, capabilities=[MCP(...) per
  server, ProcessHistory(...), ...])`, `run_stream_events(prompt,
  message_history=...)`; text, thinking, calls and results off the stream,
  the state off the result. Both keep what "nothing phones home" and
  "nothing is written down" pinned: the endpoint from the configuration,
  the key as an argument and a header, no retries, the loggers held, tracing
  off. Both take, at construction, the tool servers and their secrets beside
  the models and the keys, and an injectable seam for the tools in the
  tests (plain functions, as the examples use).
- **Datastore** (`datastore/schema.sql`, `datastore/conversations.py`):
  `runs.engine_state bytea`; `end_run` writes it; `engine_state` reads it;
  `SCHEMA_SHA256` re-pinned, `SCHEMA_VERSION` still 1.
- **Configuration and composition** (`core/models_config.py`, `app.py`):
  `[models.*].context_window`; the engines built with the models, the keys,
  the servers and the tool secrets; no MCP adapter, no `tool_servers` port,
  no `max_tool_rounds`; the import contracts allow `mcp`,
  `langchain_mcp_adapters` and `fastmcp` under the agent adapters and the
  MCP contract for `adapters/tools/mcp/` goes with the sub-package.
- **API, wire, frontend**: nothing changes. A call's arguments arrive in one
  `TOOL_CALL_ARGS`, which the client already handles.
- **Tests**: the contract suite (`contracts/agents.py`) is rewritten for the
  new port: a streamed answer, an answer that was not streamed, a tool round
  the framework runs, the state coming back and going in, a failure, a
  cancellation, and holding nothing afterwards. `fakes/agents.py` scripts the
  five events. The adapters' own tests script their framework's model as
  today and hand the adapters plain-function tools. `engines.py`,
  `test_engine_swap.py`, the swap in `test_create_app.py`, the tool-servers
  fake and contract, `test_mcp_adapter.py`, `test_tool_naming.py` go.

## Order of work

One branch, one commit per step, each leaving the unit suite green. The
Postgres suite is run where a database is to hand.

1. **The plan and the dependencies.** This file, the progress file,
   `pyproject.toml` and the lock.
2. **The domain, core and the port.** The events, the port, the renames,
   `context_window`, the checks; the fake engine and the contract suite
   rewritten; the domain and core tests.
3. **The datastore.** `engine_state` in both stores, the contract suite, the
   schema pin.
4. **The application.** The round, the state at the parent, the lifecycle
   tests rewritten for the five events.
5. **The LangChain adapter**, its tests and its live test.
6. **The Pydantic AI adapter**, its tests and its live test.
7. **The deletions and the composition root.** The MCP client, the port,
   the fakes and contracts; `app.py`; the import contracts and the
   architecture probes; the composition and route tests.
8. **The specs**: ADR 0005, and the sentences below.
9. **The checks** and the progress notes, with the line count.

## Spec sentences to change

- [specs/core.md](../specs/core.md): "The agent engine is a port" -- the
  controller knows the port and nothing of the loop; "Persistence is
  framework-neutral" becomes "The transcript is the platform's, the memory
  is the framework's"; the one-page "Tools" and "A turn" bullets; the seams
  list names the MCP clients as the frameworks'.
- [specs/legacy/agents.md](../specs/legacy/agents.md): rewritten -- the port, a turn, the
  frameworks' share of tools and context, the configuration sketch without
  `prefix` and with `context_window`, the known findings.
- [specs/legacy/runs.md](../specs/legacy/runs.md): "Tools" -- the framework runs the loop
  inside the run; a run stopped in the middle leaves the transcript as it
  was and no memory; the order of a run's events is unchanged.
- [specs/legacy/conversations.md](../specs/legacy/conversations.md): the format is the
  transcript; the model's memory is the framework's, stored on the run and
  never read; what crosses a swap: nothing, a conversation stays with its
  engine; the vendor's signed blocks are in the memory and not in `extras`.
- [specs/wire.md](../specs/wire.md): a call's arguments arrive whole.
- [specs/operations.md](../specs/operations.md): `context_window` among the
  limits; what a tool server sees.
- [specs/legacy/backend.md](../specs/legacy/backend.md): the schema is still the
  platform's; the memory is a column of it.
- [layout.md](../layout.md): the ports list, the adapters section, the
  responsibilities table, the enforcement list, the discard test.
- [ADR 0005](../adr/0005-the-framework-owns-the-loop-and-the-memory.md):
  supersedes ADR 0004 and the rest of ADR 0002's decision.
- [DEPENDENCIES.md](../../DEPENDENCIES.md): the known exclusions become
  "adopted pending a decision".

## Open

- **The licences** of `mcp`'s tree (`cffi`, `pywin32`) and of what
  `fastmcp` and `langchain-mcp-adapters` bring: the user's decision when the
  pull request is ready. The gate is expected red until then.
- **A turn that did not end** leaves no memory (decision 3). A state event
  per loop step is the remedy if a person's cancelled turn turning out to be
  forgotten by the model matters in practice.
- **`openai` and `openai-compatible`** are still refused: nothing here
  adds `langchain-openai` or the Pydantic AI `openai` extra, which is the
  same licence question as before and a dependency change of its own.
