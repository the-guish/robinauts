# Tools over MCP: progress

The plan is [mcp-plan.md](mcp-plan.md). The process is the three-agent
recipe (`recipes/three-agent-steps.md`, beside this repository), one stacked
branch per step, reviewed and committed one at a time. The codebase map is
"What exists" in [poc-progress.md](poc-progress.md) and
[model-selection-progress.md](model-selection-progress.md); this file
records only what the tools add to it. The plan's steps 4 and 5 are split
here into 4a-c and 5a-c, so that each branch stays around five hundred
lines; the plan's numbering is kept where a step is named.

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
  application (`application/turns.py`) hands the path and `()` for tools and
  fails a turn on the first tool event with `NO_TOOLS_YET`, until step 5c.
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
  and `ToolServerAuth` (`bearer` | `basic`), `DEFAULT_TOOL_TIMEOUT_SECONDS`
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
  the credential a `Bearer` or `Basic` header built from `ToolServerSecrets`,
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
- Medium: 3 (0/3), all for step 5b, which is where the tool list's shape is
  settled: the framework's Anthropic profile **rewrites a tool's schema**
  (`AnthropicJsonSchemaTransformer` strips `title` and `$schema` at every
  depth), so the two engines send different bytes for one `ToolDefinition`
  where the spec promises byte-identical lists -- pin the profile's
  transformer off in `chat_model` and assert the wire schema, or say what
  is stripped; the framework **retries once** on Anthropic's "block is
  bound to a different conversation" refusal for the models that bind
  thinking blocks (`block_binding = drop`, reported through `warnings`),
  which is the "ask the vendor to drop what it can no longer match" of the
  plan's open question happening on this engine and not on the other --
  document it, correct the plan's entry, decide whether to make it
  deliberate; a **tool result with empty text** goes to Anthropic as an
  empty `text` block, which the API refuses (langchain-anthropic sends
  `content: ""` for the same record) -- what an empty result is stored as
  is 5c's, the MCP adapter's, and the live test's.
- Low: 5 (1/4) — fixed in 4d: the test repeating `BLOCK_NOT_REPLAYED`'s
  text. Left: the test fixtures put JSON on a call's start and hand whole
  arguments where the real client never does (the engine handles the real
  shape, verified by the reviewer's probes; a test mirroring it is 5b's);
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

Review: pending.

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

Review: pending.

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
