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
