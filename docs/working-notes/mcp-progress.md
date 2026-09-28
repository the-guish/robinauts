# Tools over MCP: progress

The plan is [mcp-plan.md](mcp-plan.md). The process is the three-agent
recipe (`recipes/three-agent-steps.md`, beside this repository), one stacked
branch per step, reviewed and committed one at a time. The codebase map is
"What exists" in [poc-progress.md](poc-progress.md) and
[model-selection-progress.md](model-selection-progress.md); this file
records only what the tools add to it. The plan's steps 4, 5 and 6 are
split here into 4a-d, 5a-e and 6a-b, so that each branch stays around five
hundred lines; the plan's numbering is kept where a step is named.

## What exists

- ADR 0004: context management belongs to the agent adapter, superseding
  the paragraph of ADR 0002 that put it above the port, and closing the
  question ADR 0002 left open for the day tools came (no framework
  persistence is needed). The specs say what tools are as behaviour rather
  than plans: `agents.md` ("Agents", "The agent port", "A turn", "Tools",
  the prose of the configuration sketch), `runs.md` ("Behaviour", "Tools",
  the order of a run's events), `conversations.md` (the tool row of the
  content table, the tool message under "A turn is a chain", the vendor's
  signed blocks in `extras` under "Reasoning" and "What crosses a swap"),
  `wire.md` (the four AG-UI tool events, under the call's own id),
  `layout.md` (the `ToolServers` port, the MCP adapter and its import rule,
  the responsibilities table), `core.md` (goal 3, the one-page, "Planned")
  and `operations.md` (one identity per deployment, what the operator
  configures).
- The MCP Python SDK is **not** a dependency: its tree fails the licence gate
  (`cffi` states `MIT-0`, `pywin32` a family only), so the adapter to come is
  a client of our own over `httpx`. `adapters/tools/mcp/` exists, empty, and
  the import contract confining the SDK to it is in `pyproject.toml` with the
  probe tests of `test_architecture.py`.
- **The format carries tools.** `domain/tools.py`: `MAX_TOOL_NAME_CHARS`
  (64, the vendors' rule `[A-Za-z0-9_-]`), `checked_tool_name`,
  `MAX_CALL_ID_CHARS` (128) and `checked_call_id` (one printable word).
  `domain/conversation.py`: `ToolCallPart(call_id, name, arguments)` and
  `ToolResultPart(call_id, text, is_error)`; `SUPPORTED_PART_KINDS` is text,
  reasoning and the two; `SUPPORTED_ROLES` is every role, `tool` included;
  `Message.extras` (a bounded object, keyed by vendor, kept as a copy) and
  `Message.tool_calls` / `tool_results`; the role rules (`_check_content_of`:
  a tool message holds results only, a user message no tool part, an answer
  no result, each call id once per message); `kept_parts` keeps calls and
  refuses a result. `domain/values.py`: `checked_data` and the
  `MAX_EXTRAS_*` bounds moved here from core, shared by `extras` and a call's
  arguments. `AnswerCompleted.extras`. Core: the two parts' encodings
  (`tool_call`: `call_id`, `name`, `arguments`; `tool_result`: `call_id`,
  `text`, `is_error`), a message's `extras` written when non-empty and read
  back into the record (a part's and an event's are still read past),
  `check_answers_calls` (a tool message answers exactly its parent's calls,
  once each, in any order) applied where the tree is built, and
  `check_parent` refusing a tool message under an answer that made no
  calls. API: `SentKind` and `SentRole` grew, `ContentPart` is a
  discriminated union of `TextContent`, `ToolCallContent` and
  `ToolResultContent` with every field required, and the OpenAPI snapshot
  follows; the AG-UI mapper still refuses a `tool` announcement (the wire
  step maps it).
- **The port is handed the whole path and the tools.** `ports.agents`:
  `run_turn(agent, history, tools, *, model)`, the history the full visible
  path and never trimmed above the port (ADR 0004; `core.trim_history`,
  `DEFAULT_HISTORY_CHARS` and `Turns(history_chars=)` are gone, so the
  lines of `poc-progress.md` that name them are history). `domain/tools.py`:
  `ToolDefinition(name, description, input_schema, annotations)`, the full
  name checked by the vendors' rule, the schema and the annotations bounded
  plain data. `domain/turn.py`: `ToolCallStarted(call_id, name)`,
  `ToolCallArgumentsDelta(call_id, text)`, `ToolCallCompleted(call)` and
  `WaitingOnTools()`, held to their order by `core.check_engine_events`. The
  application (`application/turns.py`) handed the path and `()` for tools
  and failed a turn on the first tool event with `NO_TOOLS_YET`, until the
  loop of step 5e.
- **The LangGraph engine calls tools.** `adapters/agents/langgraph/engine.py`
  binds the run's tools in Anthropic's own shape (`bind_tools` passes it
  through; the engine serves only `ChatAnthropic`), announces a call as the
  client lifts it off the stream, streams its arguments, completes it with
  what they parse to, and ends the turn `WaitingOnTools`; it replays an
  answer's calls as the framework's `tool_calls` and a tool message as one
  `ToolMessage` per result; the vendor's `thinking` and
  `redacted_thinking` blocks come out in `extras["anthropic"]["thinking"]`
  and go back only to the model that made them (`THINKING_BLOCKS`,
  `VENDOR`). Its context policy is still "everything".
- **The Pydantic AI engine calls tools.** `adapters/agents/pydantic_ai/engine.py`
  declares the run's tools as an `ExternalToolset` (the framework's kind for
  tools something else runs; the turn still ends at the model's first
  answer, so the framework never looks for one), translates the stream's
  `ToolCallPart` starts, deltas and ends into the platform's call events,
  replays an answer's calls as the framework's `ToolCallPart`s and a tool
  message as one `ToolReturnPart` per result (`outcome="failed"` for an
  error), and keeps the vendor's signed blocks in `extras` under the
  framework's name for the vendor (`Model.system`, `VENDOR` for the one
  client it builds) in the same two shapes as the other engine, replaying
  them as `ThinkingPart`s only to the model that made them. Both engines'
  contract tool test runs; nothing above the port changed.
- **A call no tool message answers is shown as one that was not run** (step
  4d): `domain.NOT_RUN`, `domain.unanswered_calls`; both adapters add the
  tool turn the vendor requires after such an answer, never stored.
- **The configuration knows tool servers.** `domain/agents.py`:
  `ToolServerConfig(id, url, secret_env, auth, user, prefix, timeout_seconds)`
  and `ToolServerAuth` (`bearer` | `basic`, and `none` from step 8),
  `DEFAULT_TOOL_TIMEOUT_SECONDS`
  (60), `MAX_TOOL_TIMEOUT_SECONDS`, `MAX_BASIC_USER_CHARS`;
  `AgentDefinition.tools` (server ids, each once); `ModelsConfig.tool_servers`
  with the whole-configuration proof (an agent's servers exist; no two
  servers share a prefix). `domain/tools.py`: `TOOL_NAME_SEPARATOR` (`__`),
  `MAX_TOOL_PREFIX_CHARS` (32), `is_tool_prefix` / `checked_tool_prefix` (the
  vendors' charset, no `__`, not ending in `_`). `core/models_config.py`
  reads `[mcp_servers.<id>]` (`TOOL_SERVER_KEYS`) and an agent's `tools`,
  every problem together; `mcp_servers` is one of `MODEL_KEYS`.
  `adapters/config_file.py`: `check_tool_secrets` reads every server's
  secret at start-up into a `ToolServerSecrets` that prints nothing, and
  every secret check prints a variable's name only when it is spelt as
  variables are (`named`), so a pasted token is never echoed;
  `app.py` gathers its problems with the rest and holds the result
  (`Deployment.tool_secrets`) for the adapter to come. `agents.md` carries
  the sketch (one `bearer` server, one `basic`, an agent naming both), read
  by `tests/integration/test_config_file.py`.
- **The `ToolServers` port, and the naming above it.** `ports/tool_servers.py`:
  `ToolServers.list_tools(server) -> Sequence[ListedTool]` and
  `call_tool(server, name, arguments) -> ToolResult`, the names the server's,
  the credential the implementation's, a server that cannot be reached a
  `ToolServerError` (`domain/errors.py`), a tool's error a result.
  `domain/tools.py`: `ListedTool(name, description, input_schema,
  annotations)` (the tool as listed, its name any one line up to
  `MAX_LISTED_TOOL_NAME_CHARS`) and `ToolResult(text, is_error)` (bounded as
  the part it becomes). `core/tools.py`: `named_tools(server, listed)`
  (the full name `<prefix>__<name>`; left out with a `LeftOut` naming the
  tool and the reason when the full name is not one the vendors take, the
  schema is not an object at the top, or the server listed it twice),
  `tools_for_run(servers, listed)` (every server's, sorted by full name),
  `split_tool_name(full)`. `tests/fakes/tool_servers.py`:
  `MemoryToolServers` (scripted listings, answers, servers that are gone, a
  call that waits on an event); `tests/contracts/tool_servers.py`: the
  port's contract suite, which the MCP adapter's tests subclass next.
- **The MCP adapter.** `adapters/tools/mcp/client.py`: `McpToolServers`, the
  port over Streamable HTTP as a client of our own over `httpx` -- one
  session per question (`initialize`, the `initialized` notification, the
  one request, `DELETE`), answers read as JSON or as an event stream, the
  session id and the negotiated protocol revision carried on every request,
  the credential a `Bearer` or `Basic` header built from `ToolServerSecrets`
  (or no header at all under `none`, from step 8),
  no redirects, bodies bounded (`MAX_RESPONSE_BYTES`), listings paged and
  bounded (`MAX_PAGES`), a call bounded by the server's `timeout_seconds`
  into an error result, a JSON-RPC error on a call an error result, every
  other failure a `ToolServerError` naming the server and the exception's
  type and never the request; a result's text parts joined with notes for
  the rest and cut to a part's bound; `httpx`'s and `httpcore`'s loggers
  held at `WARNING` when the adapter is built. `tests/unit/test_mcp_adapter.py`
  drives it through a scripted Streamable HTTP server over
  `httpx.MockTransport` and subclasses the port's contract;
  `tests/live/test_mcp_live.py` reaches Microsoft Learn's public server.
- **A run's tool events.** `domain/turn.py`: `CallStarted(run_id,
  message_id, call_id, name)`, `ArgumentsDelta(..., call_id, text)`,
  `CallCompleted(..., call_id)` inside an assistant message, and
  `ResultLanded(run_id, message_id, call_id, text, is_error)` inside a
  **tool** message announced under the answer that made the calls; stored
  and sent in the one written form (`core/conversation_format.py`:
  `call_started`, `arguments_delta`, `call_completed`, `result_landed`);
  `core.check_event_order` holds a stream to them (one call at a time,
  each once, the answer completing with exactly the calls it announced and
  the streamed arguments parsing to the stored ones; a result once per call
  of the answer before, the tool message completing with exactly the
  results that landed; a slice may begin inside either). Both stores
  complete a tool message as they complete an answer (no provenance: it is
  the platform's own). The wire refuses the four kinds loudly until the
  step that maps them (`NOT_MAPPED_YET` in `test_agui.py`).
- **The tool loop.** `application/turns.py`: `Turns(tool_servers=,
  servers=, max_tool_rounds=)`; the tools of the agent's servers listed
  once per run, in parallel, before the engine is asked (`_tools_for`, over
  `core.tools_for_run`; a server that will not list fails the run naming
  it, a tool left out is logged by name); `_produce` runs the engine from
  the stored history, and when an answer asked for tools stores it with its
  calls, announces the one tool message of the batch under it, runs the
  calls in parallel through the port, publishes each `ResultLanded` as it
  lands, completes the tool message with the last (after
  `core.check_answers_calls`), lets the engine of that round go and runs
  the engine again from the history read back -- the same path a run taken
  up again takes -- until an answer asks for nothing or
  `DEFAULT_MAX_TOOL_ROUNDS` (25) is reached, which fails the run with
  `TOO_MANY_ROUNDS`. A name the run was not handed is answered
  `NO_SUCH_TOOL` (an error result, the model's mistake); a result with no
  text is stored as `NO_CONTENT`; a tool's error is an error result; only
  a `ToolServerError` fails the run, and a cancellation or a failure in the
  middle of a batch leaves the answer and its calls stored with no tool
  message, the remaining calls cancelled. The round holds the engine to the
  order the stream is read back by: a call completes as it was announced,
  its streamed arguments parse to the stored ones
  (`core.check_call_arguments`, the one rule in its three places), an
  answer completes with exactly the calls it announced. `app.py` builds
  `McpToolServers` over `Deployment.tool_secrets` at `open` and closes it
  with the rest, unless a `tool_servers` port was handed in
  (`Deployment.configured(tool_servers=, servers=, max_tool_rounds=)`).
  `tests/fakes/agents.py`: `ScriptedAgent.then(*steps)` scripts the next
  turn of a run separately from the first.
- **The wire carries tool calls.** `api/agui.py`: `CallStarted` is
  `TOOL_CALL_START` (the call's id, the tool's full name, the answer as
  `parentMessageId`), `ArgumentsDelta` is `TOOL_CALL_ARGS` (empty ones
  skipped), `CallCompleted` is `TOOL_CALL_END`, `ResultLanded` is
  `TOOL_CALL_RESULT` (the tool message's id, the call's id, the text as
  `content`, `role: "tool"`, and `metadata: {"isError": true}` on an error
  result -- `ERROR_FLAG`, the one thing this wire puts in `metadata`, since
  AG-UI 1.0's result has no field for it); a tool message announced or
  completed sends nothing, so those positions carry no `id:`; each of the
  four closes an open stretch of thinking as text does; nothing is derived
  and no state is kept for a call. `wire.md` says the same of the error
  flag. `tests/unit/test_stream_routes.py` runs a whole tool round through
  the route (the ids, the bodies, the two silent positions) and the
  re-attach property test has a `tools` shape, cutting inside a call's
  arguments, on the silent positions and on a result.
- **The frontend shows tool calls.** `agui/events.ts` decodes the four
  events (`TOOL_CALL_START` with the call's id, the tool's full name and the
  answer; `TOOL_CALL_ARGS`; `TOOL_CALL_END`; `TOOL_CALL_RESULT` with the
  tool message's id, the text as `content` and `metadata.isError` read as
  the one thing it takes out of `metadata`); a known type with wrong fields
  is ignored, as ever. `state.ts`: a `ChatToolCall` part (id, name,
  `argsText` as it streams, `args` once whole or from the store, `result`,
  `isError`) inside the answer that made it; the reducer opens the answer
  a call names if its start went missing, treats a start already held, an
  end or a result twice, and arguments for a call never announced as the
  no-ops the wire promises (and returns the same object for them); a
  stored tool message is **folded** into the answer before it (`folded`,
  `answered`) -- no bubble of its own -- and the answer keeps its id as
  `resultsId`, which `under`, `storedParent` and `upTo` use so that a
  question, an edit or a cut after that answer hangs under the tool
  message in the store. `runtime.tsx` hands a call to assistant-ui as its
  `tool-call` part (`asThreadMessage`), which the vendored Thread draws
  with `ToolFallback` -- name, arguments and result each in a text node --
  and an edit's parent goes through `storedParent`. Tests: `events.test.ts`,
  `runtime.test.tsx` (a round through the reducer, the no-ops, folding, the
  parent rules, the part handed over, an edit after a round through the
  hook), `Chat.test.tsx` (a call drawn as text: markup in the name, the
  arguments and the result stays text and makes no element). The frontend
  typecheck, red since step 3 on the `tool` role, is green again.
- **A public server, and the demo.** `ToolServerAuth.NONE` (`auth = "none"`):
  a server sent no credential names no `secret_env` (the record and the
  parser refuse one), the start-up secrets check passes it over, and the MCP
  adapter sends no `Authorization` header for it. `agents.md`'s sketch shows
  Microsoft Learn's public server as the third beside GitHub's and
  Atlassian's, `operations.md` says what such a server sees, and
  `demo/robinauts.toml.in` carries Learn's and GitHub's tables and a `tools`
  line per agent commented out, with `demo/README.md` ("Tools") saying how
  to turn Learn on, what the demo then reaches, and why the demo hands no
  token to GitHub's.

## Corrections to the plan

Found by the reviews and written into the plan where they belong, with the
date.

- Decision 8 promised "no reasoning text enters a message"; the vendor's
  signed block carries the thinking's text with its signature, so the
  promise is that the blocks are never **read** as reasoning. What is done
  with blocks that do not fit `extras` is an open question of the plan,
  met in step 4.
- The API bullet derived a tool call's wire id from the message id and the
  position; a call's id is the vendor's, stored on the part and sent as it
  is, so that a result and a loaded conversation name one call one way.

## Steps

### Step 1 — decisions on paper   (feature/mcp-1-decisions)

Summary: ADR 0004 and the spec sentences the plan lists, written as
behaviour. Nothing in the code changes; the code still trims above the port
and refuses a tool call, and the specs now describe where the steps below
take it. The ADR index gains 0003, which was missing from it, and 0004. The
TOML sketch of a `[mcp_servers.*]` table moved from this step to step 5a:
the spec-example tests parse every TOML block in `agents.md`, and the parser
does not know the table yet; the keys are described in prose meanwhile.

Review: 1 round.
- High: 1 (1/0) — decision 8's "no reasoning text enters a message" was
  not a promise the platform can keep; reworded in `conversations.md` and
  corrected in the plan.
- Medium: 6 (6/0) — whose id a tool call has (the vendor's, said once in
  `agents.md`, aligned in `wire.md` and the plan); the run's "persisted as
  it is produced" sentence made to match one tool message per batch;
  prefixes unique across servers, said; goal 3 of `core.md` names the tool
  servers; blocks that do not fit `extras` added to the plan's "Open"; ADR
  0002's open question closed in ADR 0004, and a quotation attributed to it
  that it did not contain corrected.
- Low: 9 (4/5) — fixed: `runs.md` no longer claims tools are built before
  step 5, a tool call is a part and its result a message, the run stays
  `running` through the loop, tool messages are ordinary elsewhere, the
  agent's first bullet names its tool servers, the operator's list names
  the servers and the rounds bound is not a setting yet, the strikethrough
  in ADR 0002 replaced by a note. Left for the final review: "before
  anything is written" as a phrase, the duplicated context-policy paragraph
  in `agents.md`, the component lists in `layout.md` and `core.md` that do
  not name the MCP adapter.

Checks: `reuse lint`; the four spec-example tests of
`tests/integration/test_config_file.py`, which parse `agents.md`, pass
again with the sketch out; the whole suite against a throwaway PostgreSQL
otherwise unchanged (2969 passed, 13 skipped, before the review's fixes).
Not done / to watch: until step 4 the code contradicts `agents.md` on
trimming, and until step 5 on tools; the plan's "Order of work" says so.

### Step 2 — the dependency   (feature/mcp-2-dependency)

Summary: the MCP Python SDK's tree was put through the licence gate on a
copy of the lock, and **fails**: `cffi` (through `pyjwt[crypto]` and
`cryptography`) states `MIT-0`, which is on no list, and `pywin32`
(Windows-only) states a licence family and no licence. Everything else in
the tree passes. The plan's fallback for this case is taken: no dependency
is added, the two packages and the SDK are rows of DEPENDENCIES.md's "Known
exclusions", the specs say the adapter is a client of our own over `httpx`,
and the import contract confining the SDK to `adapters/tools/mcp/` is
written before the fact -- with the probe tests the framework rules have,
which prove a probe importing an uninstalled `mcp` still breaks it -- and
the sub-package exists, empty but for its docstring.

Checks: `tests/unit/test_architecture.py` (12 passed, the SDK's three
probes among them); the licence gate over the unchanged lock; lint.
Not done / to watch: the client of our own is step 5b's, and is what the
plan warned about -- a hand-written parser of a protocol -- so its review
reads the SSE reading and the session handling first. Two decisions would
let the SDK in and are a person's, recorded in the plan's "Open": `MIT-0`
on the allowed list settles `cffi`, and `pywin32` -- runtime, Windows-only,
family-only metadata -- needs a policy answer of its own.

Review: 1 round.
- High: 0
- Medium: 4 (4/0) — the `cffi` row dated the licence change wrongly (2.1.0,
  not 2.0); the notes said one edit would let the SDK in (it takes two); the
  port bullet of `layout.md` still said "over the SDK"; the decisions the
  step met are the plan's "Open" now, not a line in these notes.
- Low: 4 (4/0) — the `mcp` row names the whole of the tree that passes; a
  comment cited the wrong section; the probe's docstring said "framework";
  these notes say why the steps are lettered, and the plan's logging note
  names `httpx`'s loggers rather than the SDK's.

### Step 3 — the format   (feature/mcp-3-format)

Summary: the two tool parts, the `tool` role carried, `extras` kept on a
message and on a completed answer, the tree rule that a tool message answers
exactly its parent's calls, and the wire's content schemas grown to serve
the new parts (the spec-example test couples `SentKind` to the supported
kinds, so the schema and the OpenAPI snapshot moved here from step 6). The
plain-data check that `extras` had in core moved to the domain so a tool
call's arguments share it. Nothing produces these messages yet; a stored one
round-trips through both stores (`contracts/conversation_store.py`). Larger
than the five hundred lines a step aims for, by the snapshot and the tests.

Review: 1 round.
- High: 1 (0/1) — step 4a's uncommitted edits were in the tree the reviewer
  read; step 3's commit holds none of them, so nothing to fix.
- Medium: 7 (5/2) — fixed: the tree refuses a second tool message under one
  answer (the spec's "never side by side" is a rule now); the module header
  of `conversation_tree.py` names the rule and no longer says the role is
  refused; a route test serves a turn with tools as data, and the events'
  round trips carry a tool message and an answer's `extras`; the mapper's
  comment. Left: `AnswerCompleted.extras` is not carried to the stored
  message yet (step 4a, the application's step); the engines' docstrings
  still say the role is not translated (steps 4b and 4c rewrite them).
- Low: 6 (4/2) — fixed: `checked_data` takes a mapping of another kind as
  an object; `extras: null` reads as nothing; a broken stored tool part is
  a fault of ours; the vendor ids checked against the call-id rule are
  noted below. Left: reads validate `extras` and arguments twice (once in
  the encoding, once in the record), a cost accepted for one rule in one
  place; a module-header line about `check_parent` reading content.

Checks: lint; the whole suite against a throwaway PostgreSQL (3019 passed);
the frontend's regenerated types and its 413 tests.
Not done / to watch: `core.runs._check_completed` accepts a completed tool
message but does not yet hold it to its parent's calls or to what was
published for it, and **both stores refuse to `complete_message` a tool
message** (they demand an assistant message with the run's provenance),
which step 5c relaxes with the contract suite; the AG-UI mapper refuses a
tool announcement until step 6a; `datastore/schema.sql`'s comment on the
`tool` role still says "reserved and refused" and is left alone, since
every edit of that file re-pins `SCHEMA_SHA256` and makes operators
recreate their database. Gemini sends no tool call id at all, so an adapter
reaching it would have to mint one (Pydantic AI does); the call-id rule
takes Anthropic's, OpenAI's, Bedrock's and Pydantic AI's minted ids.

### Step 4a — the port, the tool engine events, trimming gone   (feature/mcp-4a-port)

Summary: `run_turn(agent, history, tools, *, model)`, the history the full
visible path and never trimmed above the port (ADR 0004): `core.trim_history`,
`DEFAULT_HISTORY_CHARS` and `Turns(history_chars=)` are gone, and
`test_conversation_history.py` with them. `ToolDefinition` (the full name,
a description, the input schema and the MCP annotations, bounded). Four
engine events -- `ToolCallStarted`, `ToolCallArgumentsDelta`,
`ToolCallCompleted`, `WaitingOnTools` -- and `check_engine_events` holding
them to the order: a call inside an answer, one at a time, its streamed
arguments parsing to the part it completes with, the answer completing with
exactly the calls it announced, and an answer that asked for tools followed
by `WaitingOnTools` and by nothing else. The fake engine scripts a tool call
(`fakes.calls`), the contract suite asks for one (`Say.calls`, skipped by an
engine whose `can_call_tools` is off), and both real engines take the tools
argument and refuse a non-empty one until they bind (steps 4b and 4c). The
application hands the whole path and no tools, fails a turn on the first
tool event so nothing half-answered is stored, and carries a completed
answer's `extras` on to the stored message (from step 3's review).

Review: 1 round (read after the commit; the fixes ride with step 4b).
- High: 0.
- Medium: 3 (2/1) — fixed in 4b: `check_engine_events` refused streamed
  arguments nested past what the parser follows with a `RecursionError`
  instead of the `InvalidValueError` it promised, and compared them as
  Python (`1 == 1.0 == True`) rather than as JSON; a lifecycle test now
  holds the stored answer to the `extras` the engine handed back. Left for
  the final review: ADR 0004 and `agents.md` say the **contract suite**
  keeps the one invariant above the port (the question is whole in what
  the model sees) while the port docstring and the code keep it in each
  adapter's own tests -- either the contract grows a hook for what the
  scripted model was shown, or the two documents say what the code does.
- Low: 9 (6/3) — fixed in 4b: the port's "how it ends" names
  `WaitingOnTools`; the whole-path test's question is the longest message
  (LangGraph; Pydantic AI's in 4c); a half-character split across two
  argument deltas is tested; the never-announced lifecycle test asserts the
  error and the readable stream; the engine checks what it is handed are
  `ToolDefinition`s; `NO_TOOLS_YET` speaks of this version, not this
  deployment, and of tool usage, not the loop; the prose of the format
  module no longer says a tool call is refused; the contract test is named
  for what it does (the model asks for no tool). Left: `fakes.calls`'s
  `streamed` governs the arguments, not the text (rename or widen when a
  test needs it); Pydantic AI's stale prose (4c); `poc-progress.md` (noted
  under "What exists" instead of edited).

Checks: lint; the whole suite against a throwaway PostgreSQL (3026 passed,
15 skipped).
Not done / to watch: the real engines still refuse a tool call from the
model (`NO_TOOLS`) and a turn handed tools (`NO_TOOL_BINDING`) until steps
4b and 4c, where each also gets its context policy and its own history
tests; the application's refusal (`NO_TOOLS_YET`) is what step 5c replaces
with the loop.

### Step 4b — the LangGraph engine calls tools   (feature/mcp-4b-langgraph)

Summary: the engine binds the run's tools in Anthropic's own shape (`name`,
`description`, `input_schema`, which `bind_tools` passes through untouched
to the one client this engine serves) and never runs one. Streaming: the
client lifts a call off the stream as `tool_call_chunks` -- id and name on
the first chunk, pieces of JSON with no id after -- and the engine announces
it, streams the pieces, completes it with what they parse to (or with the
framework's parsed arguments when nothing streamed), refuses arguments that
are not JSON or not an object, refuses arguments for no announced call, and
refuses a `tool_use` block in the final message that was never announced
rather than dropping it. An answer that only calls holds the calls and no
text; an answer that calls ends the turn `WaitingOnTools`. History: an
answer's calls travel as the framework's `tool_calls`, a tool message as one
`ToolMessage` per result with its `status`, which langchain-anthropic folds
into the one `user` turn of `tool_result` blocks the vendor wants. The
vendor's signed blocks (`thinking`, `redacted_thinking`) come out of the
final message into `extras["anthropic"]["thinking"]` as they were, the
thinking's text is streamed as reasoning deltas as it arrives and never
stored as content, and the blocks are replayed in front of the answer's
content only when the run's model is the one that made them
(`provenance.model`); a block that does not fit `extras` is left out with a
line in the log, and the answer is stored without it (the plan's open
question (a), for this engine: dropped and logged, so a later turn on the
same model sends that answer without its thinking). `NO_TOOLS`,
`TOOL_BLOCKS` and `NO_TOOL_BINDING` are gone from this engine;
`can_call_tools` is on, so the contract's tool test runs against it. The
scripted chat model of the tests binds tools, records what was bound and
makes the real client's chunks (`calling`). The fixes from step 4a's review
ride here (above).

Review: 1 round (read after the commit; the fixes ride with step 4d).
- High: 0.
- Medium: 5 (2/3) — fixed in 4d: **a user question under an answer whose
  calls have no result** (a stopped or failed tool round, then the ordinary
  next question) went to the vendor as calls with nothing answering them,
  which Anthropic refuses whole; both adapters now answer such a call with
  what the record says (`domain.NO_RESULT`, `unanswered_calls`), and the
  record is untouched; the non-streamed tests tested the fixture (the
  scripted model handed a chunk where the real client hands a plain
  message, so the real non-streamed path had no coverage). Left:
  `bind_tools` drops a tool whose schema has a
  top-level `anyOf`/`oneOf` with a Python warning and raises when every
  tool is dropped — step 5a's naming and bounding leaves such a tool out
  with a line in the log before an engine sees it; a history with tool
  parts on a run handed **no** tools (an agent whose server was removed)
  may be refused by Anthropic, which wants `tools` whenever the messages
  hold `tool_use` — unverified offline, for the live test; the 64 KiB
  `extras` bound makes "dropped and logged" the common case on a
  long-thinking tool turn (a block is about four bytes a token) — for the
  final review, as the plan's open question says.
- Low: 7 (6/1) — fixed in 4d: the `no cover` pragma on the plain-message
  path (it is the real non-streamed path); `_parsed` catching
  `RecursionError` in both engines; `BLOCKS_LEFT_OUT` exported and used by
  the tests; the whole-path test's question strictly the longest; the
  prose on a call that streamed no arguments; one spelling of the vendor
  metadata in the tests. Left: an empty result text and an empty tool
  description reach the vendor as `""`, accepted by the client and not
  checked live.

Checks: lint; the whole suite against a throwaway PostgreSQL (3036 passed,
14 skipped).
Not done / to watch: the adapter does not turn thinking **on** -- the
blocks are handled when a model sends them (a `thinking` parameter on the
model, or a vendor default), and nothing tests the live client end to end
(`tests/live/` still runs a plain turn); a live test with tools and
thinking on is step 5b's or the final review's. No prompt caching, no
token counting, no trimming: the context policy is "everything", as ADR
0004 allows for now. A dropped oversize block is never asked back from the
vendor; if Anthropic refuses an answer replayed without its thinking, the
turn fails loudly and the fix is a policy in `_assistant`, not the store.
The Pydantic AI engine still refuses tools (step 4c).

### Step 4c — the Pydantic AI engine calls tools   (feature/mcp-4c-pydantic-ai)

Summary: the engine declares the run's tools to the framework as an
`ExternalToolset` -- the framework's own kind for tools it does not execute,
so the model is shown them as any tool and nothing in the framework could
run one -- and the turn still ends at the model's first answer, which is
also what keeps the framework's own loop and its retry prompt out. The
stream's `PartStartEvent`/`PartDeltaEvent`/`PartEndEvent` for a
`ToolCallPart` become `ToolCallStarted`, `ToolCallArgumentsDelta` and
`ToolCallCompleted` (a call is completed when its part ends, when the next
part begins or when the answer ends; arguments the framework hands over
whole are published as one JSON piece, so what was published still parses
to the stored call); arguments that are not JSON or not an object fail the
turn; a call of a kind the engine did not declare (the vendor's server-side
tools, another class in the framework) is refused rather than passed over;
a call the framework holds that was never announced is refused rather than
dropped. History: a question is a `UserPromptPart`, an answer is its signed
blocks (replayed as `ThinkingPart`s only when the run's model made them),
its text and its calls as `ToolCallPart`s, a tool message is one
`ToolReturnPart` per result named after the call in the answer before it
(`outcome="failed"` for an error, which the framework sends as
`tool_result` with `is_error`); a result naming no such call, or a tool
message holding anything but results, is a fault of ours. The vendor's
signed blocks come out of the framework's response into `extras` under the
framework's name for the vendor (`Model.system`: `anthropic` for the one
client this engine builds, the key the other engine uses, so a conversation
crosses the swap with its thinking) in the same two shapes
(`{"type": "thinking", "thinking", "signature"}` and
`{"type": "redacted_thinking", "data"}`); an unsigned thinking part is
streamed and not kept; blocks that do not fit are left out with a line in
the log; a stored block of another shape is left out with a line in the log
rather than handed to a framework that would send it as text. `NO_TOOLS`
and `NO_TOOL_BINDING` are gone; `can_call_tools` is on for both engines and
the contract's tool test runs against both. The tests grow a `Vendor` model
that speaks as the vendor's client does where `FunctionModel` cannot (a
redacted block, whole arguments, the vendor's name), and one test maps the
translated history through the framework's real Anthropic mapping to check
the blocks the vendor is sent.

Review: 1 round (read after the commit).
- High: 0.
- Medium: 3 (2/1) — fixed in 5b: the framework's Anthropic profile
  **rewrites a tool's schema**
  (`AnthropicJsonSchemaTransformer` strips `title` and `$schema` at every
  depth), so the two engines send different bytes for one `ToolDefinition`
  where the spec promises byte-identical lists -- pin the profile's
  transformer off in `chat_model` and assert the wire schema, or say what
  is stripped; the framework **retries once** on Anthropic's "block is
  bound to a different conversation" refusal for the models that bind
  thinking blocks (`block_binding = drop`, reported through `warnings`),
  which is the "ask the vendor to drop what it can no longer match" of the
  plan's open question happening on this engine and not on the other --
  documented in 5b (pinned off; the framework's retry documented in the
  engine). Left: a **tool result with empty text** goes to Anthropic as an
  empty `text` block, which the API refuses (langchain-anthropic sends
  `content: ""` for the same record) -- what an empty result is stored as
  is the loop's and the live test's (5e).
- Low: 5 (2/3) — fixed in 4d: the test repeating `BLOCK_NOT_REPLAYED`'s
  text; fixed in 5b: a test of a call whose start carries no arguments,
  the real client's shape. Left: the fixtures still put JSON on a call's
  start elsewhere (the engine handles the real shape, verified by the
  reviewer's probes);
  "the vendor's server-side tools are refused" overstates it, since the
  real client drops an unrequested `server_tool_use` before the parts
  manager sees it; no test has two results in one tool message or maps a
  calls-only answer through the vendor mapping; the "next part begins" and
  "answer ends" completions are fallbacks the framework's part-end events
  make unreachable.

Checks: lint; the whole suite against a throwaway PostgreSQL (3050 passed,
13 skipped).
Not done / to watch: as for 4b, thinking is not turned on by the adapter
and no live test runs a tool turn; the framework may transform a tool's
JSON Schema for the vendor (its profile's transformer), which is its
mapping and not checked here; the `_map_message` test reaches into the
framework's private API, as the other engine's `_format_messages` test
does, and breaks when the framework renames it. The plan's open question
on oversize blocks is recorded as answered (dropped and logged, both
engines).

### Step 4d — a call no tool message answers   (feature/mcp-4d-unanswered-calls)

Summary: a step the 4b review added. A stored answer that asked for tools
and has no tool message under it on the path -- the turn was stopped, or
failed, before its results were in -- followed by the next question, went
to the vendor as `tool_use` blocks with nothing answering them, which
Anthropic refuses whole; once step 5c stores calls, every later turn of
such a branch would fail. Both adapters now answer such a call with what
the record says: after that answer they put the tool turn the vendor
requires, one error result per unanswered call whose text is
`domain.NO_RESULT` (reworded from "not run" by the review, see below), which
is never stored (the record keeps the calls without results; the client
shows exactly that). Which calls those are is
`domain.unanswered_calls` (a path rule: an answer's calls are answered when
the next message on the path is a tool message). Each engine's test maps
the result through the framework's real vendor mapping. The fixes from
step 4b's review ride here (above): the LangGraph scripted model's
non-streamed path hands over a plain `AIMessage` as the real client does,
the tests expect the no-delta shape and `can_answer_without_streaming` is
on for LangGraph too.

Review: 1 round (read after the commit; the fixes ride with step 5b).
- High: 0.
- Medium: 3 (3/0), fixed in 5b: the sentence said the call **was not run**,
  which the record cannot know (a batch runs in parallel and the tool
  message is written when the last result is in) and which would invite the
  model to make the call again -- it now says no result was recorded and
  whether the call ran is not known (`NO_RESULT`); `unanswered_calls` had no
  test of its own -- a table test over the path shapes; the decision was in
  the working notes and nowhere the layout or the specs promise -- a clause
  in `layout.md`'s domain list, a sentence in the module's docstring, the
  spec sentence in `runs.md` "Tools".
- Low: 6 (5/1) — fixed in 5b: the 4b entry's counts; the stale comment on
  the off-contract path in the Pydantic AI engine; the LangGraph double's
  docstring claims only the type of the real client's message; ADR 0004 is
  no longer cited for a choice it does not make; the plan's step 7 notes
  that `unanswered_calls` trusts the tree and would compare call ids under
  a partial tool message. Left as a note: Pydantic AI's framework would
  synthesize a sentence of its own for a dangling call not in the last
  response; with the adapter's inserted, nothing dangles (recorded in the
  plan).

Checks: lint; the whole suite against a throwaway PostgreSQL (3052 passed,
13 skipped).
Not done / to watch: whether Anthropic takes a history with tool parts on a
run handed no `tools` at all is still unverified (4b's review, M4).

### Step 5a — the configuration knows tool servers   (feature/mcp-5a-config)

Summary: `[mcp_servers.<id>]` and an agent's `tools`, as `agents.md`
describes them, from the file into the records. A server is its `url`
(checked as every configured endpoint is; the value is never echoed, since
it may hold the very credential the check refuses), `secret_env` (the
**name** of the variable; a secret pasted there is refused), `auth`
(`bearer` by default, or `basic`, which names the `user` part and refuses
a `:` in it; a `user` under `bearer` is refused), `prefix` (the vendors'
charset, at most 32 characters, no `__`, not ending in `_`; the server's
id when left out, and an id that would not do as a prefix -- a long one, or
one holding `__` -- must have one written) and `timeout_seconds` (per tool
call, 60 by default, bounded as a model's is). An agent's `tools` is a list
of configured server ids, each once; two servers under one prefix are
refused by both names; a server with a mistake in it does not bury it under
the agents naming it (the declared table is what `tools` is checked
against, as a model's provider is). The records hold the same rules
(`ToolServerConfig`, `AgentDefinition.tools`, `ModelsConfig.tool_servers`),
so a caller building one by hand meets them too. Start-up reads every
server's secret by variable name into `ToolServerSecrets` (a carrier that
prints nothing, `ProviderKeys`' twin), reports every unset one together
with the rest of the file's problems, and does not connect to any server.
The spec's sketch is a fourth TOML block that the configuration tests read
and parse. Step 5 is re-cut on the way: 5a is this; 5b the `ToolServers`
port, the listed tool as a record, naming/sorting/bounding in core and the
in-memory fake; 5c the MCP adapter over httpx with its scripted-server and
live tests; 5d the loop in `Turns`, the turn events and the composition.

Review: 1 round (read after the commit; the fixes are a commit of their
own on step 5b's branch, "mcp 5a: fixes from the review").
- High: 1 (1/0) — a **GitHub token pasted into `secret_env`** is letters,
  digits and underscores, which is a valid variable name as far as the
  spelling rule can tell, so it was accepted as a name and then **echoed by
  the start-up refusal** into the log. Fixed where the message is written
  (`adapters.config_file.named`, used by all three secret checks): a name
  spelt as variables conventionally are -- upper case, digits, underscores
  -- is printed, and anything else is described rather than repeated. The
  claim above that "a secret pasted there is refused" holds for a token with
  a `-` in it and not for one without; the test says so now.
- Medium: 1 (1/0) — the sketch's Atlassian line pointed at a Jira site host,
  where no MCP endpoint answers; it is `https://mcp.atlassian.com/v2/mcp`.
- Low: 5 (5/0) — stale docstrings (`domain/agents.py`, four records and two
  carriers); the prefix clash is found over the **declared** tables and the
  `:` in a basic user part in the parser, so both are reported in the same
  pass as the table's other mistakes; a user part under a misspelt auth is
  not a second mistake, and an unknown server id named twice is one unknown
  id; the prefix tests assert the rule's wording; the composition test
  asserts its string edit found its line.

Checks: lint; the whole suite against a throwaway PostgreSQL (3098 passed,
13 skipped).
Not done / to watch: the secrets are read and held (`Deployment.tool_secrets`)
and handed to nothing until 5c; a `[mcp_servers]` table in a deployment
with no agent naming it still needs its secret at start-up (as a provider no
model uses needs its key); the demo's TOML is step 8's.

### Step 5b — the port, the listed tool, the naming, the fake   (feature/mcp-5b-port)

Summary: what step 5c's adapter implements and step 5d's loop calls. The
`ToolServers` port asks two questions of one configured server -- what it
lists (`ListedTool`s under the server's own names) and what a tool answers
(`ToolResult`, text and whether the server calls it a failure) -- with the
credential the implementation's (built with `ToolServerSecrets`), a tool's
error, a timeout and an unknown tool all results, and only a server that
cannot be reached or will not list a `ToolServerError` naming it
(`docs/specs/runs.md`, "Tools"). Naming lives in core, pure: the full name
`<prefix>__<name>`; a tool left out, named with the reason, when its full
name is not one the vendors take, when its schema is not a JSON Schema
object at the top (the vendors' rule, and the top-level `anyOf` that
`bind_tools` would otherwise drop with a Python warning -- 4b's review), or
when the server listed it twice; one list per run sorted by full name so
two engines and two runs send identical lists; a full name split back at
the first separator, `None` for a name the platform never gave. The
in-memory fake scripts listings, answers, servers that are gone and a call
that waits; the contract suite holds the fake and, next, the adapter to
the same promises. Riding here from the reviews: 4d's rename to
`NO_RESULT` with its table test and its three documents; 4c's pinning of
the framework's schema transformer off, so the Pydantic AI engine sends a
tool's schema as the server gave it (tested), the documented framework
retry on bound thinking blocks, and the test of a call whose start carries
no arguments (the real client's shape).

Review: 1 round (read after the commit; the fixes ride with step 5d).
- High: 0.
- Medium: 3 (1/2) — fixed in 5d: the spec's "Tools" names the two other
  reasons a tool is left out (a schema that is not an object at the top;
  listed twice). Left for 5e and the final review: the contract suite does
  not hold an implementation to the timeout, the cancellation or the
  arguments reaching the server (each implementation proves those with its
  own clock or gate; the suite's prose now says so); an **empty result
  text** is a value the domain accepts and the port does not speak for --
  settled in 5e, where the loop stores results.
- Low: 8 (6/2) — fixed in 5d: the contract's prose named the wrong hooks
  and a check it does not make; the vendors' bound is named by its
  constant; the notes' counts for 4c and the plan's port path; protocol
  names kept out of the domain's and the port's prose; the unknown-tool
  wording promised by the port. Left: `named()` still repeats a "name"
  spelt wholly in upper case, digits and `_` (an AWS access key id has
  that shape); the schema-pin test compares dicts, not bytes.

Checks: lint; the whole suite against a throwaway PostgreSQL (3137 passed,
13 skipped).
Not done / to watch: nothing implements the port against a server yet
(5c); an empty result text (4c's review, M3) is still a question for the
adapter and the live test; `tools_for_run` takes what each server listed
and is told nothing about a server that failed to list, which the loop
turns into a failed run before calling it (5d).

### Step 5c — the MCP adapter over httpx   (feature/mcp-5c-adapter)

Summary: the one `ToolServers` implementation, a client of our own for the
three calls a client needs, since the SDK's tree fails the licence gate.
Streamable HTTP as the protocol has it: one `POST` per JSON-RPC message to
the server's URL, `Accept: application/json, text/event-stream`, the answer
read as a JSON body or as an event stream whose `message` events are read
past notifications and unrelated messages to the response with our id; a
session per question -- `initialize` (offering `2025-06-18`, refusing a
revision this build does not know), the `initialized` notification, the
request, and a `DELETE` when the server handed out an `Mcp-Session-Id`,
which goes back on every request with `MCP-Protocol-Version`. Pinned as the
vendor clients are: the URL is the configuration's, the credential a header
built from what start-up read, no redirects, no retries, `HTTPS_PROXY`
obeyed, `httpx`/`httpcore` loggers at `WARNING`. Bounded before parsed: a
body past `MAX_RESPONSE_BYTES`, a listing past `MAX_PAGES` pages, a
result's text past a part's bound (cut with a note saying how much). What
comes back: `tools/list` entries as `ListedTool`s (an entry this build
cannot carry left out with a line in the log naming the server and the
entry's position, never its content); `tools/call` results as their text
parts joined, a note for an image, audio, a resource without text, a link or
a kind unknown, `structuredContent` as JSON when there is no content,
`isError` as the flag; a JSON-RPC error on a call (an unknown tool, bad
arguments) and a call past the server's `timeout_seconds` are error
results; a server that cannot be reached, refuses the credential (401/403,
said by status), answers another status, something that is not the protocol
or an unknown revision, or closes the stream without answering is a
`ToolServerError` naming the server and the exception's type, never the
request. The tests script a Streamable HTTP server over `httpx.MockTransport`
(JSON and event-stream modes, sessions or none, pages, a hanging tool,
refusals, bare bodies) and subclass the port's contract suite; the live test
lists and calls Microsoft Learn's public server, which needs no credential.

Review: 1 round (read after the commit; the fixes are a commit of their
own on step 5e's branch, "mcp 5c: fixes from the review").
- High: 3 (3/0) — the body was **read whole and then measured**, so the
  bound was a post-hoc check and an endless body was never refused: the
  client now sends every request with `stream=True`, refuses a declared
  length over the bound and a content encoding it did not ask for
  (`Accept-Encoding: identity`), counts the bytes as they arrive off the
  wire, and reads an event stream only as far as our answer; the
  **credential was reachable** from the session's `repr`, from a
  traceback's locals and from the raised error's `__context__`: the session
  holds a callable that builds the header when a request is built and has
  no `repr`, the request is deleted before a failure is raised, and the
  failure is raised outside every `except`, chained to nothing (tested
  with `capture_locals`); the session's **`DELETE` was awaited during a
  cancellation and after the call's time**: it is bounded by the connect
  timeout, skipped when the task is being cancelled, and the call's
  `timeout_seconds` now holds over the session it takes and the goodbye.
- Medium: 3 (3/0) — a stream the server keeps open (pings) held the read
  until the read timeout; incremental parsing returns at our answer, and
  a whole-listing ceiling (`LISTING_SECONDS`) bounds the question; a status
  that is not the protocol's on a **call** (404, 429, 5xx) is an error
  result the model is told, the listing and the session staying strict; the
  untested branches (a JSON-RPC error on the listing, an answer for another
  request, a bare carriage return, a byte-order mark, protocol-level error
  codes, the goodbye's bound) have tests.
- Low: 6 (6/0) — dead field and stale `noqa`s; the logging pin's reason
  says what those loggers write (URLs, response headers); the `DELETE`
  carries the protocol version; the truncation note counts the cut made;
  the protocol-level JSON-RPC codes (-32700, -32600, -32601) fail the call
  as the server's, and the revision before this one (batches) is refused;
  the leading byte-order mark is read past.
  The fixes were checked with lint, the import contracts, the adapter's
  tests (44) and the live test; the whole suite runs again with step 5e.

Checks: lint; the whole suite against a throwaway PostgreSQL (3165 passed,
13 skipped); the live test against `https://learn.microsoft.com/api/mcp`
from this machine (1 passed: a real listing of three tools and a real
`microsoft_docs_search` call, over an event-stream answer with a session
id).
Not done / to watch: a server that takes **no** credential still needs a
`secret_env` in the configuration and is sent a bearer token it ignores
(Microsoft Learn does); an `auth = "none"` is a spec question for step 8 or
the final review. An empty result text is stored as `""` (4c's review, M3:
what the vendors make of it is for the loop's live turn). No cache in front
of `tools/list` (the plan's backlog). The client does not resume a stream
(`Last-Event-ID`) and does not handle a server-to-client request inside a
stream (it passes it over and the stream ends when the server answers).

### Step 5d — the run's tool events, the order, the stores   (feature/mcp-5d-events)

Summary: what the loop of step 5e publishes and what the wire of step 6a
maps, settled first and alone so that each of those steps is one thing. A
tool call is inside the answer that makes it -- `CallStarted` with the
vendor's id and the tool's full name, `ArgumentsDelta`s as the model writes
the JSON, `CallCompleted` -- and a result is inside the one tool message of
the batch, announced under that answer (`MessageStarted` with the `tool`
role) with a `ResultLanded` per call as it lands and completed when the last
is in; every one of them has the platform's written form, versioned with the
rest, and `check_event_order` holds a stream to the order (`runs.md`,
"Behaviour" and "Tools"): one call at a time, each id once per message, an
answer completing holding exactly the calls it announced with the streamed
arguments parsing to the stored ones (as JSON, sharing the engine check's
rule), a result once per call of the answer before it, a tool message
completing holding exactly the results that landed, text and flag alike,
and a tool message never under an answer that made no calls; a slice may
begin inside a call or inside a tool message, adopting the call that was
open at the cut. Both stores complete a tool message as they complete an
answer, with the contract test; a question is still never completed by a
run. The AG-UI mapper does not map the four kinds yet and refuses them as a
mistake of ours; the closed-set test names them as not-yet, for step 6a to
take off the list. The fixes from step 5b's review ride here.

Review: 1 round (read after the commit; the fixes are a commit of their
own on step 5e's branch, "mcp 5d: fixes from the review").
- High: 1 (1/0) — a slice cut **between two `ArgumentsDelta`s** of a call
  was refused: the call adopted at the cut had the tail of its arguments
  compared as if it were the whole. The adopted call is remembered and its
  arguments are not compared (what was streamed before the cut was not
  seen); a test walks every cut of the round.
- Medium: 5 (4/1) — one call is adopted at a cut, not one after another
  (M2); a whole run cannot begin with a tool message under the question
  (M3); a tool message completes holding a result **for every call** of the
  answer before it, and the loop asks `check_answers_calls` of the stored
  pair before the write, so the tree never refuses what a run stored (M4);
  the ten rules the order tests did not pin (a mutation run found them)
  each have a case (M5). **Left**: a message adopted in a slice is not
  checked at its completion beyond its streamed arguments (M1) — for the
  final review, with the note that a re-attaching watcher is exactly who a
  slice is for.
- Low: 7 (2 fixed, 2 in part, 3 left) — what follows an answer that made
  calls is decided and held: the tool message, and never the end of a
  finished run (L5); the dead reset at a message's announcement is gone and
  the order check's docstring no longer says a run produces only answers
  (L2, L3 in part: `_check_results`' unreachable branch, the format's
  unreachable default, and the stale lines in `api/agui.py` and
  `ports/conversations.py` wait for 6a and the final review). Left: one
  adoption helper for the four sites (L1), a delta inside a tool message
  (L4), the export asymmetry (L6), the spec's additivity rule not naming a
  new event kind (L7).
  The fixes were checked with lint and the unit suite; the whole suite runs
  again with step 5e.

Checks: lint; the whole suite against a throwaway PostgreSQL (3189 passed,
13 skipped).
Not done / to watch: nothing publishes these events yet (5e); the wire does
not map them (6a); `MessageStarted` for a tool message passes
`check_supported_role`, and the mapper's `sent_role` still refuses the
`tool` role until 6a maps a tool message to nothing of its own.

### Step 5e — the tool loop and the composition   (feature/mcp-5e-loop)

Summary: the first turn that calls a server. `Turns` is given the
`ToolServers` port, the configured servers and `max_tool_rounds`, and
refuses an agent whose servers are not configured or a deployment with
nothing that reaches them. `_produce` is the loop of the plan (decisions 5,
6, 7 and 9): the agent's servers listed once per run, in parallel, before
the engine is asked, named and sorted by `core.tools_for_run` and the
left-outs logged by name; the engine run from the stored history; an answer
that asked for tools stored with its calls, the batch's one tool message
announced under it, the calls run in parallel through the port, each
`ResultLanded` published as it lands, the tool message completed with the
last after `core.check_answers_calls`; the engine of that round let go and
run again from the history read back -- the one path a run taken up again
takes -- until an answer asks for nothing, or the bound is reached and the
run fails with `TOO_MANY_ROUNDS`. A call for a name the run was not handed
is answered with `NO_SUCH_TOOL` as an error result; a result with no text
is stored as `NO_CONTENT`; a tool's error is an error result; a
`ToolServerError` fails the run naming the server; a cancellation or a
failure mid-batch leaves the answer and its calls stored, no tool message,
and the calls still running cancelled. The round holds the engine to what
the stream is read back by (a call completes as announced, its streamed
arguments parse to the stored ones through `core.check_call_arguments`,
the answer completes with exactly the calls it announced, each call id
once, nothing after the turn ends waiting and no answer after one that
asked for tools), so that what an engine yields out of order fails the
run before anything the stream could not read back is stored. `app.py`
builds `McpToolServers` over the secrets at `open` and closes it with the
rest, or takes a port handed in (`Deployment.configured(tool_servers=,
servers=, max_tool_rounds=)`). `ScriptedAgent.then` scripts the second
turn of a run. Tests: `test_turn_lifecycle.py` ("the tool loop": one
round end to end, the listing once per run and the history read back, the
next turn's path, results published as they land, a failing tool, a name
not handed, an empty result, a server that will not list, a server gone
mid-batch, a cancellation mid-call with `unanswered_calls` on the record,
the bound, an answer completing with calls it never announced);
`test_app_composition.py` (the adapter built and closed; a port handed in
reached through the deployment's own `turns`). The fixes from the 5c and
5d reviews are commits of their own on this branch.

Review: 1 round (read after the commit; the fixes are a commit of their
own, "mcp 5e: fixes from the review", on step 6b's branch).
- High: 1 (1/0) — an engine that yielded **a second answer after one that
  asked for tools**, or anything after `WaitingOnTools`, was accepted: the
  run finished, the calls were never run, and the stream was one
  `check_event_order` refuses. The round holds the engine to the order
  `check_engine_events` states -- nothing after the turn ends waiting, no
  answer after one that asked for tools -- and fails the run before
  anything is stored; a table test drives each rule.
- Medium: 4 (4/0) — a call id named twice in one answer is refused (M1); a
  round after the results that yields no answer fails the run with
  `NO_ANSWER_AFTER_TOOLS`, since a run finished on a tool message is one
  nothing can continue from (M2); the calls still running when a batch is
  abandoned are cancelled and waited for **bounded** by `CLOSING_SECONDS`,
  as the engine is, and abandoned with a line in the log past that (M3);
  `Deployment.configured` refuses servers handed in without the port that
  reaches them and a handed-in agent naming a server that will not be
  wired, as it refuses models without engines (M4).
- Low: 5 (5/0) — a listing that fails cancels its siblings (L1);
  `NO_SUCH_TOOL` covers what its docstring says: a name not among the tools
  the run was handed is answered without a server being asked (L2); tests
  for the rules the round enforces (a call completed under another name,
  streamed arguments that do not parse to the stored ones, waiting after an
  answer with no calls, an answer completed with a call open, a bound of
  zero) (L3); the module docstring says what a run with tools writes, the
  stray literal is gone, the note's claim is honest and the plan's "before
  anything is written" reads "before the engine is called" (L4); a result
  of nothing but whitespace is stored as `NO_CONTENT` (L5).

Checks: lint; the import contracts; the whole suite against a throwaway
PostgreSQL (3220 passed, 13 skipped).
Not done / to watch: the wire does not map the four events (6a); the
frontend shows nothing of a call (6b); no `waiting` state -- a call whose
result does not come inside the run is a cancelled or failed run with the
calls stored (plan, step 7); a name the run was handed whose server has
no such tool goes to the server, whose refusal is the error result; the
`auth = "none"` question for a server that needs no credential (step 8).

### Step 6a — the AG-UI tool events   (feature/mcp-6a-wire)

Summary: the four tool events on the wire, as `wire.md` promised them.
`AguiMapper.of` maps `CallStarted` to `TOOL_CALL_START` under the call's
own id with the tool's full name and the answer as `parentMessageId`,
`ArgumentsDelta` to `TOOL_CALL_ARGS` (an empty one skipped, as an empty
text delta is), `CallCompleted` to `TOOL_CALL_END`, and `ResultLanded` to
`TOOL_CALL_RESULT` naming the tool message and the call, with the text as
`content` and `role: "tool"`; a result that is an error carries
`metadata: {"isError": true}` (`ERROR_FLAG`), which the spec now names as
the one thing this wire puts in `metadata`, because AG-UI 1.0's result has
no field for it and a person watching wants to see a failed call without
reloading. A tool message announced or completed sends nothing -- it is
not a text message in AG-UI, and its results went out one by one -- so
`sent_role` is never asked for `tool` and those two positions carry no
`id:`, which the re-attach rule already covers (a client re-attaches at
the last id it saw, and is replayed nothing it had). Every tool event
closes an open stretch of thinking, as text does; nothing is derived and
no state is kept for a call, the ids being the platform's own. The
closed-set test covers all ten kinds; `NOT_MAPPED_YET` is gone. Tests:
`test_agui.py` (the four events' bodies, the error flag, a tool message
sending nothing, thinking closed by a call and by a tool message, an empty
arguments delta skipped, no `metadata` anywhere but the flag);
`test_stream_routes.py` (a whole tool round through the route with the
ids and bodies checked, and the drop-anywhere re-attach property over a
turn with a tool round before its answer). The OpenAPI document is
unchanged: the streaming routes are outside it, and the content parts
were step 3's.

Review: 1 round (read after the commit; the fixes are a commit of their
own, "mcp 6a: fixes from the review", on step 6b's branch).
- High: 0.
- Medium: 2 (2/0) — the commit's lint was red (a 128-character docstring
  line) while the note said "lint": re-wrapped, and this note stands
  corrected; the thinking closed before `TOOL_CALL_ARGS` and
  `TOOL_CALL_END` was untested (a mutation run showed it): the closers
  test now drives all four tool events and the three of a tool message.
- Low: 4 (4/0) — `ERROR_FLAG` in its place in `__all__`; the module
  docstring's "no `metadata`" reads "nothing in `metadata` but the error
  flag"; a vacuous assertion in the route test removed; `wire.md` says a
  tool message sends nothing **announced or completed**, and the property
  test's docstring says the cuts fall *across* the silent positions.

Checks: lint; the whole suite against a throwaway PostgreSQL (3220 passed,
13 skipped).
Not done / to watch: the frontend decodes none of it yet (6b); the mapper
sends a result's text as it is, and it is the client that renders it as
data (`wire.md`); `api/agui.py`'s `UNMAPPED` docstring is true again.

### Step 6b — the frontend decodes tool events and draws calls as data   (feature/mcp-6b-frontend)

Summary: the chat shows what a turn did with its tools. `events.ts` decodes
`TOOL_CALL_START`, `TOOL_CALL_ARGS`, `TOOL_CALL_END` and `TOOL_CALL_RESULT`
as the backend writes them, the error flag read out of `metadata` and
nothing else from there, a result whose content is not one string ignored
(this backend sends one). The state holds a call as a part of the answer
that made it (`ChatToolCall`: the vendor's id, the full name, the
arguments as text while they stream and as data once whole, the result
and its flag), and the reducer keeps the wire's no-ops -- a start already
held, an end or a result twice, arguments for a call it never saw -- as
no-ops that hand back the same object, opening the answer a call names
when its start went missing as it does for text. A stored tool message is
folded into the answer before it rather than drawn as a bubble, and the
answer remembers its id (`resultsId`): a new question, an edit and a cut
after that answer go to the tool message in the store (`under`,
`storedParent`, `upTo`), since a message sent under the answer itself
would leave the results off the path the model sees. The runtime hands a
call to assistant-ui as its `tool-call` part, which the vendored Thread
draws with `ToolFallback` behind a "1 tool call" trigger -- the name, the
arguments and the result each in a text node, as `wire.md` requires of
attacker-influenced text -- and an edit's parent goes through
`storedParent`. Tests: `events.test.ts` (the four events, the flag, wrong
fields ignored), `runtime.test.tsx` (a round through the reducer, an error
result, the no-ops, a cancellation mid-batch, folding and the parent
rules, the part handed over, an edit after a round through the hook),
`Chat.test.tsx` (markup in a name, in arguments and in a result stays
text and makes no element). The frontend's typecheck, red since step 3
added the `tool` role to the API's enum, is green again.

Review: 1 round (read after the commit; the fixes are a commit of their
own, "mcp 6b: fixes from the review", on step 8's branch).
- High: 0.
- Medium: 3 (3/0) — **regenerating the answer after a tool round** cut
  the thread at the message before it on the screen, the calling answer,
  which stayed on the screen under the new answer until the re-read: a
  regeneration replaces the turn, so the cut is at the turn's question
  (`turnStart`); **a call without a result in a completed answer was
  drawn as done** -- during the batch, and after a cancellation or a
  failure in the middle of one: an answer whose calls are unanswered stays
  `running` until its last result lands, a run ending before that marks it
  as it marks any answer left open, and a conversation read back shows
  such an answer as the run left it (running with a run in flight, else
  cancelled, or failed with the sentence); **`isError` was decoded and
  handed over and nothing drew it**: a `ToolCall` component composed from
  the vendored fallback's parts, outside `vendor/`, gives a failed call the
  icon of one and a line saying the tool answered that the call failed.
- Low: 4 (4/0) — the guards a mutation run found unpinned have cases (a
  cut under a tool message in the middle of a thread, a call naming no
  answer, an end for a call nothing holds); a tool message after anything
  but an answer is dropped, as the comment said; the calling answer is no
  longer rebuilt on every re-read (the arguments are compared as data,
  since the store's spelling of the JSON is not the model's); the dead
  fallback on the arguments is gone and this entry's claim about
  "Cancelled tool" is true now.

Checks: frontend typecheck, lint, prettier, tests (424); the backend suite
runs again with the 5e and 6a fixes on this branch (3238 passed, 6 skipped).
Not done / to watch: a call's status is the message's, which stays running
while the batch is; two answers of one turn are two bubbles, the round's
answer with its calls and the answer after; nothing renders `args` beyond
the fallback's text; the demo and the operator's page (step 8).

### Step 8 — the demo's tool servers, a public server, the operator's page   (feature/mcp-8-demo)

Summary: what an operator copies. The open question step 5c's live test
raised is answered: `auth = "none"` for a server that takes no credential
(`ToolServerAuth.NONE`), which names no `secret_env` -- the record and the
parser refuse one, saying there is no variable to name -- is passed over
by the start-up secrets check, and is sent no `Authorization` header at
all by the MCP adapter, rather than an empty one a server might read as a
credential. `agents.md` says the third way the secret is sent and shows
Microsoft Learn's public server as the third table of the sketch, which
the integration test reads; `operations.md` says such a server sees the
deployment's address and the arguments the model wrote, and nothing that
names anyone. `demo/robinauts.toml.in`
carries Learn's table, GitHub's table and a `tools` line under each agent,
commented out, with a paragraph saying what turning them on does, and
`demo/README.md` gains a "Tools" section: Learn's server needs no
credential and is a `#` away, the demo then reaches `learn.microsoft.com`,
and GitHub's needs a token the demo does not carry, since `start.sh`
strips only the provider's key from what it starts. Tests: the parser
(a server with `none`, one that names a variable all the same, one with a
user part), the record, the adapter (no header for a public server), the
deployment (no variable read for one).

Review: 1 round (read after the commit; the fixes are a commit of their
own, "mcp 8: fixes from the review").
- High: 0.
- Medium: 2 (2/0) — under a misspelt `auth` a missing `secret_env` was
  reported as a second problem, which would invite a variable the next
  start refuses: it is read only when present, as the user part is (M1);
  the demo's README said a request to Learn carries "nothing about you",
  when it carries the query the model wrote from the question: the README
  and `operations.md` say what is sent (M2).
- Low: 7 (7/0) — the 6a entry's Checks block, orphaned under step 8, is
  back in its place; the older notes and plan lines that said two schemes
  say three; the README and the template say the call is behind a toggle
  rather than "shown"; the token advice says plainly that the demo cannot
  hand GitHub's server a token safely; the record's messages say
  `auth = "none"` rather than "no auth"; `check_tool_secrets` passing a
  public server over is tested where the check is tested; `sends_alone`
  is in its place in `__all__`.

Checks: lint; the import contracts; the whole suite against a throwaway
PostgreSQL (3240 passed, 6 skipped); the live test against Microsoft Learn with no credential.
Not done / to watch: the demo does not exercise a tool server itself (the
lines are commented); `start.sh` passes no token through on purpose; a
server that needs a credential of another scheme (a custom header) is
still a scheme to add.

## Where it stands

Every step of the plan is implemented, reviewed once each and its Highs
fixed, with the fixes as commits of their own on the branch of the step
after. Step 7 (a run that suspends on a tool) is deferred as the plan says;
approval before a tool runs is in the backlog. The branches, one per step
and stacked in this order:

`feature/mcp-1-decisions`, `-2-dependency`, `-3-format`, `-4a-port`,
`-4b-langgraph`, `-4c-pydantic-ai`, `-4d-unanswered-calls`, `-5a-config`,
`-5b-port`, `-5c-adapter`, `-5d-events`, `-5e-loop`, `-6a-wire`,
`-6b-frontend`, `-8-demo`; `feature/mcp-plan` is the head of the whole
work, and each fixes commit sits on the branch of the step after the one
it fixes.

**The commits carry no DCO sign-off.** They were written by an agent, which
cannot certify the DCO in its own name (CONTRIBUTING.md refuses a
pseudonymous sign-off), and CI's `check-dco` job fails a pull request
without one. Whoever takes the work over certifies it as their own after
reading it, in one pass over the range::

    git rebase --exec 'git commit --amend --no-edit --reset-author -s' de45ca3

which makes them the author and the committer of every commit and adds
their sign-off; `git cherry-pick -s` does the same one commit at a time.

**Deferred from the reviews**, for whoever takes it up (each is a Medium or
a Low the step's review judged not to block): 4b's tool history on a run
whose agent has no tools (M4) and its history test's shape (M5); 5b's
contract hooks (M2); 5d's adopted message not checked at its completion
(M1), one adoption helper for the four sites (L1), a delta inside a tool
message (L4), the export asymmetry (L6) and the spec's additivity rule not
naming a new event kind (L7); 5a's `named()` residual (an all-upper-case
token would still be printed as a variable's name); the schema-pin byte
test of 4c. Nothing in that list changes what a person sees or what a
server is sent.

Final review: pending.
