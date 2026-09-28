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

Review: pending.

Checks: lint; the whole suite against a throwaway PostgreSQL.
Not done / to watch: the real engines still refuse a tool call from the
model (`NO_TOOLS`) and a turn handed tools (`NO_TOOL_BINDING`) until steps
4b and 4c, where each also gets its context policy and its own history
tests; the application's refusal (`NO_TOOLS_YET`) is what step 5c replaces
with the loop.
