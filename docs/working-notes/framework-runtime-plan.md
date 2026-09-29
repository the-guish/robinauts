# Plan: one agent runtime

Written 2026-09-29. Not a spec: the decisions are in
[ADR 0005](../adr/0005-one-agent-runtime.md), and the wanted behaviour is in
[specs/agents.md](../specs/agents.md), [specs/runs.md](../specs/runs.md) and
[specs/conversations.md](../specs/conversations.md), which describe the
target from this plan on. This note is what the refactor removes and adds,
the order to do it in, and how each step is checked. **It is measured by
what it deletes.** Until the steps land, the code differs from the specs as
"Where it stood" says.

## What is wanted

- The framework runs the turn: the model calls, the tool loop, the tools.
  The platform does not run a tool and does not run the engine again from
  the record.
- The framework owns the model-facing history, its context management —
  trimming, summarization, server-side compaction — and prompt caching,
  configured per agent. The platform stores that history per run, unread.
- The platform keeps what it is for: the run lifecycle (background
  execution, one active run, cancel, numbered events, re-attach), the wire,
  the tree with its edits and regenerations, the record in its own format
  for the interface, the archive and analysis, the configuration, and the
  gates (nothing phones home, no environment fallbacks, pinned clients).
- The seam stays agent-framework agnostic and the component replaceable,
  proved at contract size: one production runtime, one reference runtime
  bounded by the contract suite.
- Less code. Every step below names what it deletes.

## Where it stood (2026-09-29, at `31398ca`)

| component | lines | fate |
|---|---|---|
| `adapters/agents/langgraph/engine.py` | 1,467 | delete (step 4) |
| `adapters/agents/pydantic_ai/engine.py` | 1,487 | rewrite as the runtime, ~500–700 (step 3) |
| `adapters/tools/mcp/client.py`, `ports/tool_servers.py`, its fake and contract | 745 + ~270 | delete (step 5) |
| `core/tools.py` (naming, sorting, bounding a run's tools) | 130 | delete (step 5) |
| `domain.tools_for_request`, `unanswered_calls`, `NO_RESULT`, `ListedTool` | ~120 | delete (step 5) |
| `application/turns.py`: `_tools_for`, `_answered`, `_called`, the rounds loop, `WaitingOnTools` handling | ~300 | delete (step 2) |
| `core/runs.py`: `check_engine_events`, `check_event_order` and helpers, no production caller | ~530 | move to `tests/` (step 2) |
| `extras` replay of signed thinking in both adapters, and the rules about it in `domain` | ~150 | delete (step 3) |
| tests: `test_langgraph_engine.py` 2,249, `test_engine_swap.py`, `test_engines_over_chat_completions.py`, `test_mcp_adapter.py` 817, `contracts/tool_servers.py`, `fakes/tool_servers.py`, the parity fixtures | ~4,600 | delete; the runtime's tests and the contract suite are rewritten smaller |
| `pyproject.toml`: `langgraph`, `langchain-core`, `langchain-anthropic`, `langchain-openai`, `langsmith`; the `tiktoken` licence-text exception and, if nothing else locks `regex`, `CNRI-Python` | 5 dependencies | delete (step 4) |
| import-linter contracts naming `langgraph`, `langchain*`, `langsmith`; the `mcp` rule's home | — | delete or move (steps 4, 5) |

Rough ledger at the end: about 2,400 source lines and 5,000 test lines
deleted; about 900 source lines and 800 test lines added. Net about −5,700.

Two things in the review that this plan does **not** touch, because they
are independent of the frameworks and worth their own decision: the
persistence of every streamed delta as its own transaction (the first
performance problem the platform will meet), and the layering rule that
keeps `datastore` from calling the pure codec in `core`.

## Settled

1. **One production runtime, on Pydantic AI**, used as it is meant to be
   used: its agent, its toolsets and MCP support, its event stream, its
   history processors and compaction capabilities, its cache settings, its
   deferred-tool results. Its capabilities system is the hook surface the
   platform needs, and every hook is a public API (checked against 2.47).
2. **The record is projected, the transcript is stored.** Each run stores
   the framework's new messages for the turn as one opaque document
   (`ModelMessagesTypeAdapter` JSON, tagged `pydantic-ai` and the version).
   The next turn's native history is the slices of the runs on the visible
   path. A run without a slice contributes its messages rebuilt from the
   record.
3. **No framework persistence plugin.** No checkpointer, no session store.
   The platform's database holds both representations.
4. **A conversation stays on its runtime.** Changing an agent's engine
   reaches new conversations only. The `Engine` value of an existing
   conversation that names an engine no longer wired is rebuilt from the
   record on its next turn under the agent's current engine, once, and the
   conversation is moved; this is pre-release and no deployment carries
   such conversations.
5. **Context policy is agent configuration**: `context` on an agent naming
   what the framework does as the history grows — server-side compaction
   where the vendor offers it (Anthropic, OpenAI), a summarising history
   processor with a threshold and a summarising model elsewhere, or nothing
   — and whether prompt caching is on. Applied to the native transcript
   only. Rare, large cuts over sliding windows
   (the survey in the examples repository verifies the cache effect of each
   mechanism across four frameworks).
6. **Tools through the framework's MCP support**, with three hooks of the
   platform's: prefixing per server, a wrap of every tool execution (the
   server's timeout; later the person's credential and an audit line), and
   the event stream that publishes every call and result as it happens.
7. **The reference runtime** is a direct-SDK implementation over
   `anthropic` and `openai`, as small as the contract suite allows, offered
   by no configuration. It proves the port needs no framework at all, which
   is the strongest form of agnosticism, and it drags no framework
   dependency into a test-only path.
8. **The MCP SDK is admitted**: `MIT-0` allowed (this plan's pull request);
   `pywin32` settled by locking for Linux and macOS only.

## Steps, in order

Each step is one branch, lands green, and names what it deleted in its
pull request. The steps are ordered so that every intermediate state runs.

**0. Licence and lock.** `MIT-0` on the allowed list, the two JavaScript
rows that carried it by name removed, the exclusions table updated (this
pull request). Then, in `backend/pyproject.toml`,
`[tool.uv] environments = ["sys_platform == 'linux'", "sys_platform == 'darwin'"]`
and a relock, which takes `pywin32` and `colorama` out of the locked set;
`pydantic-ai-slim[mcp]` added once its newest release clears the ten-day
cooldown; `scripts/check-licences.sh` and `check-audit.sh` green over the
new tree — anything new that fails the gate is a decision to record here,
never a fallback to hand-written code. The `mcp` import rule moves from
`adapters/tools/mcp` to `adapters/agents/pydantic_ai`. Deletes nothing yet.

**1. The transcript at rest.** A `run_transcripts` table (`run_id` primary
key and foreign key with cascade, `runtime`, `version`, `document jsonb`);
`ConversationStore.end_run` takes the transcript beside the ended run and
its event, both or neither; `transcripts_of(run_ids)` in path order; the
fake, the contract suite, the schema hash re-pinned. About 150 lines added.
Deletes nothing.

**2. The port and the application.** `AgentRuntime.run(agent,
native_history, input, servers, *, model)` streams `EngineEvent`s: the
existing vocabulary plus `ToolResultLanded(call_id, text, is_error)` and
`TurnEnded(state, transcript)`; `WaitingOnTools` goes. A second port method,
`history_from_record(messages)`, is the lossy rebuild. `input` is the
question, or the tool results and approvals a waiting run is taken up with.
In `application/turns.py`, `_produce` becomes one call of the runtime: the
tool message is assembled from `ToolResultLanded` events under the answer
that made the calls, the slice is stored at `TurnEnded`, and `_tools_for`,
`_answered`, `_called`, the rounds loop, `max_tool_rounds` and
`ToolServers` leave the application. `check_engine_events` and
`check_event_order` move to `tests/`, where their only callers are. A fake
runtime replaces the fake agent; the contract suite is rewritten around the
new port with the three scripted turns it needs (an answer, an answer with a
call and a result, a turn that ends waiting). Deletes about 300 lines of
`turns.py` and moves 530 of `core/runs.py`.

**3. The Pydantic AI runtime.** The adapter builds, per turn, a framework
agent with: the provider client the adapter pins today (endpoint, key,
headers, no retries — `chat_model` stays); an `MCP` capability per server
the agent names, under `PrefixTools` with the server's prefix, the
credential as a header from the secrets read at start-up; a wrap of tool
execution for the server's timeout; `ProcessEventStream` translating the
framework's events into `EngineEvent`s; the agent's `context` configuration
as `AnthropicCompaction` / `OpenAICompaction` / a `ProcessHistory`
summariser, and the cache settings; `run_stream_events` with the native
history and the input; `DeferredToolRequests` ending the turn waiting;
`ModelMessagesTypeAdapter` for the slice. The projection, one way, replaces
`_messages`, `_response`, `_returned`, `_replayed`; the reverse projection
is `history_from_record`. The `_ChatCompletionsStream` overrides are
revisited one by one against the framework's current stream handling, each
kept only with a test that fails without it. Deletes the old engine's
translation both ways and its extras replay; the runtime lands at 500–700
lines.

**4. Delete LangGraph.** The sub-package, its tests, its import contracts,
its five dependencies, its `ENGINES` entry and `Engine.LANGGRAPH`, the
`tiktoken` exception row and the tripwire tests, the parity and swap tests.
The composition test asserts one production runtime wired. Deletes about
1,500 source lines and 3,000 test lines.

**5. Delete the tool port and the MCP client.** `adapters/tools/`,
`ports/tool_servers.py`, its fake and contract, `core/tools.py`,
`tools_for_request`, `unanswered_calls`, `NO_RESULT`, `ListedTool`, and the
`ToolServers` wiring in `app.py`. `ToolServerConfig` stays: it is
configuration, read by the runtime. Deletes about 1,300 source lines and
1,000 test lines.

**6. The reference runtime.** `adapters/agents/reference/` over the two
vendor SDKs, the contract suite parametrised over both runtimes, the import
contract naming it, the discard tests redefined: deleting the production
runtime breaks the configuration and the composition table; deleting the
reference breaks only the contract's second parametrisation. Adds about 400
lines and their tests.

**7. Later, on the same shape.** Approval before a tool runs and external
results: a route that delivers `DeferredToolResults` to a waiting run.
Per-user credentials: the tool-execution wrap. Gemini and Bedrock: provider
extras of the framework and two `ProviderKind`s.

## Checks at every step

`scripts/check-all.sh`: the unit suite with the architecture contracts, the
store contract suites against PostgreSQL, the licence and audit gates, the
frontend. The application's tests run over the fake runtime and must not
change between steps 2 and 6 except where a step says so. The live tests
under `tests/live/` are the only ones that reach a vendor; step 3 is not done
until they pass against Anthropic and OpenAI with tools.

## Risks and open

- **The slice format is the framework's.** A framework upgrade may change
  it. The tag carries the version, the framework's serializer reads what it
  can, and the rebuild from the record is the fallback. A test stores a
  slice at the pinned version and reads it back after every bump.
- **The MCP SDK's tree through the gate.** `pyjwt`, `cryptography`
  (`Apache-2.0 OR BSD-3-Clause`), `cffi` (`MIT-0`), `pycparser`,
  `sse-starlette`, `python-multipart`, `jsonschema` and what it brings all
  passed on 2026-09-28 at 2.2.0. Re-checked at relock.
- **Chat Completions against the Responses API** for OpenAI's reasoning
  models with tools is unchanged by this plan and stays a later decision.
- **The record no longer says what the model saw.** The slice does, and
  analytics reads both.
- **`Engine` keeps one production value.** Whether the reference runtime
  appears in the enum at all, or only in the contract suite, is decided in
  step 6.

## Spec sentences this changes

Changed in the pull request that carries this plan, so that the specs
describe the target:

- `specs/core.md`: "The agent engine is a port" and "Persistence" under
  Design principles; the seams list; "Tools" and "A turn" in the one-page
  view.
- `specs/agents.md`: the engine bullet under "Agents"; "The agent port", "A
  turn" and "Tools" rewritten; the provider bullets that named both engines;
  the configuration sketches' `engine`; "Known findings" reduced to what
  still holds.
- `specs/runs.md`: "Tools" rewritten; the LangGraph state note under
  "Details likely to change".
- `specs/conversations.md`: the format's introduction; the signed-blocks
  paragraph under "Reasoning"; "What crosses a swap" replaced by "The native
  transcript"; "Persistence"; the context note under "Details likely to
  change".
- `layout.md`: the directory tree, `core`, the `Agent` and `ToolServers`
  ports, `adapters`, the responsibilities table, the enforcement list, the
  testing strategy.
- `specs/operations.md`: the context limit; `specs/wire.md`: the tool
  arguments and the bridges sentence.
- `README.md`: the two sentences that promised two interchangeable
  frameworks and platform-run tools.
- ADR 0002 and ADR 0004 marked superseded in part or in whole; ADR 0005
  added.
