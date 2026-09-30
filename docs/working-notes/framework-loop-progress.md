# The frameworks own the loop: progress

The plan is [framework-loop-plan.md](framework-loop-plan.md). The codebase
map is "What exists" in [poc-progress.md](poc-progress.md),
[model-selection-progress.md](model-selection-progress.md) and
[mcp-progress.md](mcp-progress.md); this file records what this change
made of it, and what it took away. Built on `claude/modest-newton-69yxl6`
from the head of the MCP work, 2026-09-30, in one commit rather than the
one-per-step the plan asked for: the steps were built in the plan's order
and the suite was green at the end of each, but the deletions of step 7
were what let steps 2 to 6 collect and import, so the branch was not
committable in between.

## What exists

- **ADR 0005**: the framework owns the loop, the context and the model's
  memory; the platform keeps the transcript. Supersedes ADR 0004 and the
  rest of ADR 0002's decision (the record is still the platform's; the
  frameworks are no longer stateless per turn; a conversation stays with
  its engine). The specs say it as behaviour: `core.md` ("The agent engine
  is a port", "The transcript is the platform's; the memory is the
  framework's", the one-page), `agents.md` (rewritten: the port, a turn,
  tools, the sketch with `context_window` and without `prefix`, the known
  findings), `runs.md` ("Tools", the run's memory, the order of a run's
  events with a call's arguments in one piece), `conversations.md` (the
  format is the transcript; the memory; the signed blocks live in the
  memory; what crosses a change of engine or model), `wire.md`,
  `operations.md`, `backend.md`, `layout.md` (the port, the adapters
  section, the import rules, the responsibilities table, the discard test,
  the testing strategy).
- **The port** (`ports/agents.py`): `Agent.stream(agent, prompt, *, model,
  state) -> AsyncGenerator[Event, None]`, the examples' `AgentBackend` made
  async and stateless per call. **The events** (`domain/events.py`):
  `TextDelta`, `ReasoningDelta`, `ToolCall(call_id, name, arguments)`,
  `ToolResult(call_id, name, output, is_error)` and `Done(text, state)`,
  the state bytes bounded by `MAX_ENGINE_STATE_BYTES` (64 MiB).
  `core.check_backend_events(events, cut_short=)` holds an adapter to the
  order: text and reasoning before `Done`, a result answers a call announced
  before it once and under its name, `Done` last and once with every call
  answered. The eight engine events of `domain/turn.py` are gone; the
  platform's published pieces are `TextPiece`, `ReasoningPiece` and
  `ArgumentsPiece` (their written form unchanged, so nothing stored moved).
- **The memory on the run** (`runs.engine_state bytea`;
  `ConversationStore.end_run(..., engine_state=)` and
  `engine_state(run_id)`; the schema pin re-recorded, version still 1).
  The application (`application/turns.py`, `_memory`) resumes a turn from
  the nearest finished run of the same engine on the visible path above
  the question, so a fork of the transcript is a fork of the memory; a
  state written by another engine is not read (a line in the log, the turn
  begins from nothing); a run that did not finish stores none.
- **The application's round** (`Turns._round`): one stream per turn. A
  `TextDelta`, `ReasoningDelta` or `ToolCall` with no answer open opens
  one; a call is published as `CallStarted`, one `ArgumentsPiece` holding
  the JSON whole, and `CallCompleted`; the first `ToolResult` completes the
  answer with its calls and opens the tool message under it, each result
  is a `ResultLanded`, and the tool message completes when every call has
  its result (`check_answers_calls`); `Done` completes what is open — the
  streamed text wins over `Done.text`, which is the answer only when
  nothing was streamed — and ends the turn with the state. A turn that
  ends without an answer, or stops with an answer open or a call
  unanswered, is a failed run as before (`NO_ANSWER`, `UNFINISHED_ANSWER`).
  A `Done` arriving with calls announced and none answered is refused
  ("a turn ends with every call it announced answered"; found by the
  lifecycle tests). The loop, `_tools_for`, `_answered`, `_called`, the
  rounds bound and its failures, `NO_SUCH_TOOL`, `tool_servers`,
  `max_tool_rounds` are gone.
- **The LangChain adapter** (`adapters/agents/langgraph/engine.py`, 772
  lines, was 981): `create_agent(chat, tools, system_prompt, middleware)`
  over the tools `langchain-mcp-adapters` lists for the agent's servers
  (`MultiServerMCPClient`, Streamable HTTP, `tool_name_prefix=True`,
  `handle_tool_errors=True`; the credential from
  `adapters.credential_header`), the history read from the state plus the
  question, `astream` with `messages`, `updates` and `values`: text and
  reasoning off the model node's chunks alone (the summarising middleware
  calls the model too), calls and results off the `model` and `tools`
  nodes' updates, the final state off the last values, written with
  `messages_to_dict`. Middleware: `SummarizationMiddleware` at
  `SUMMARIZE_AT` (0.8) of the window, keeping `KEEP_MESSAGES` (20), and
  `AnthropicPromptCachingMiddleware`. The window: `context_window`, then
  the profile's `max_input_tokens`, then `DEFAULT_CONTEXT_WINDOW` (200k).
  The bound: `RECURSION_LIMIT` from `MAX_TOOL_ROUNDS` (25) and
  `STEPS_PER_ROUND` (3: the summarising step, the model, the tools);
  `GraphRecursionError` fails the turn. Everything pinned before is pinned
  still: the endpoint, the key as an argument and a header, no retries, the
  loggers held, tracing off, the two v1 variables removed.
- **The Pydantic AI adapter** (`adapters/agents/pydantic_ai/engine.py`,
  860 lines, was 1121): `Agent(model, name, instructions, toolsets,
  capabilities=[ProcessHistory(within(window))])` with one `MCPToolset`
  per server (`id`, the credential as headers, `init_timeout` and
  `read_timeout` from `timeout_seconds`, `tool_error_behavior="failed"`,
  `.prefixed(server.id)`), `instrument = False`; `run_stream_events(prompt,
  message_history, model_settings, usage_limits)`: text and thinking off
  the part events, a call off `FunctionToolCallEvent` with its arguments
  whole, a result off `FunctionToolResultEvent` (a failed return or a retry
  prompt is an error result), the state off the result's `all_messages()`
  through `ModelMessagesTypeAdapter`. The context: `within(window)` drops
  the oldest exchanges (a question and everything up to the next) while
  the rest measures over `TRIM_AT` (0.8) of the window at
  `CHARS_PER_TOKEN` (4), before every model call and on the memory handed
  back, never the latest exchange. The cache: `anthropic_cache_instructions`,
  `anthropic_cache_tool_definitions`, and `anthropic_cache` at the vendor
  or `anthropic_cache_messages` at a gateway. The bound:
  `UsageLimits(request_limit=REQUEST_LIMIT)`, `MAX_TOOL_ROUNDS + 1`;
  `UsageLimitExceeded` fails the turn. A model that answers with neither
  text nor a call is asked again by the framework once, then the turn
  fails — the framework's own retry, now wanted.
- **Configuration**: `[models.*].context_window` (tokens, optional, at
  most `MAX_CONTEXT_WINDOW`); `[mcp_servers.*]` without `prefix`;
  `adapters.credential_header(server, secrets)` is the one spelling of
  `Bearer` / `Basic` / none for both frameworks' clients. The engines are
  built as `adapter(models, keys, tool_secrets)`; an agent handed in
  without its engine is checked against the configured servers. The
  import contracts allow `langchain_mcp_adapters` under the LangChain
  adapter, `fastmcp` under the Pydantic AI adapter, and `mcp` under both.
- **Gone**: the `ToolServers` port and its fake and contract; the MCP
  client of our own (`adapters/tools/`, 745 lines) and its unit and live
  tests; `core/tools.py` and the naming rules; `ToolDefinition`,
  `ListedTool`, the old `ToolResult`, `NO_RESULT`, `unanswered_calls`,
  `tools_for_request`, `NO_LONGER_OFFERED`, `ToolServerError`; the swap
  fixtures (`tests/engines.py`, `test_engine_swap.py`, the swap in
  `test_create_app.py`); the vendor's blocks in `extras` and everything
  that carried them.
- **Tests**: the contract suite (`tests/contracts/agents.py`) rewritten to
  the eight situations of the plan, subclassed by both adapters over their
  framework's own scripted model (a `BaseChatModel` with one response per
  call; a `FunctionModel` the same) and tools that are plain functions; the
  fake engine scripts the five events (`says`, `calls`, `results`, `Gate`,
  `Raise`); the lifecycle, watch, route, composition and store suites
  rewritten for the port and the memory. 2947 tests pass.

## The numbers

Against the head of the MCP work (`git diff --numstat 5b4dbc5`, the new
files counted):

| area | added | deleted | net |
|---|---:|---:|---:|
| `backend/src` | 1604 | 3625 | −2021 |
| `backend/tests` | 2133 | 5043 | −2910 |
| docs (this plan, this note and ADR 0005 among them), `DEPENDENCIES.md`, `demo` | 1093 | 442 | +651 |
| `uv.lock`, `pyproject.toml` | 745 | 18 | +727 |
| whole | 5575 | 9128 | **−3553** |

`application/turns.py` is 2219 lines, was 2390; the two adapters together
1632, were 2102; the MCP client's 745 are gone.

## The checks

- `scripts/check-lint.sh`: green. `scripts/check-tests.sh`: green (2947
  passed, 224 skipped; the Postgres suite skips without a database).
  `scripts/check-reuse.sh`: green. `scripts/check-audit.sh`: green (109
  packages, no known vulnerability).
- `scripts/check-licences.sh`: **red, as the plan said it would be**, on
  exactly two packages, `cffi` (`MIT-0`) and `pywin32` (a family, no
  licence), both brought by the MCP SDK's `pyjwt[crypto]`; everything else
  the new tree brings passes. The decision is the user's, with the pull
  request ([DEPENDENCIES.md](../../DEPENDENCIES.md), "Known exclusions").
- `scripts/check-dco.sh`: red until the taker-over signs the commit, as
  with the MCP branch: the recipe is in [mcp-progress.md](mcp-progress.md)
  ("Where it stands").
- Not run here: the frontend and wheel gates (no change on the frontend or
  the wire), and the live tests (`tests/live/`, which need a key; the
  three are rewritten to the port and collect).

## Deferred

- **A turn that did not end leaves no memory** (ADR 0005, "Costs"). A
  state per step of the loop is the remedy if it matters.
- **Tools that outlast a process** are further off than they were: a
  suspended run is now a question for each framework's deferred-tool
  support, not for the record alone (`runs.md`, "Tools").
- The Pydantic AI adapter measures the history at four characters a token;
  the LangChain adapter with langchain-core's approximate counter. Neither
  is the vendor's tokeniser, and both trim early rather than late.
- `openai` and `openai-compatible` are still refused: nothing here changes
  the `tiktoken` question.
- The `schema.sql` comment on the `tool` role still calls it reserved; the
  schema was re-pinned for `engine_state` and the comment was left, to keep
  this change to what the plan named.
