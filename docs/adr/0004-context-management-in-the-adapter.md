# ADR 0004 — Context management belongs to the agent adapter

- Status: superseded by [ADR 0005](0005-one-agent-runtime.md); it
  superseded one paragraph of
  [ADR 0002](0002-conversation-persistence.md)
- Date: 2026-09-28

## Context

ADR 0002 decided that the platform owns the conversation record and that
both agent engines are stateless per turn, and it added a rule about where
the code that shapes a turn may live: "anything that must behave identically
under both frameworks lives above the agent port, not inside an adapter", with
"trimming or summarising a long history to fit a context window" named as the
first example. The specs repeated it
([agents.md](../specs/agents.md), "A turn";
[conversations.md](../specs/conversations.md), "Details likely to change";
[operations.md](../specs/operations.md), "Limits"), and the code followed it:
`core.trim_history` counted characters, dropped whole turns from the front of
the visible path, and the application handed each engine the trimmed suffix
(`Turns._history`, `DEFAULT_HISTORY_CHARS`).

Tools over MCP ([working-notes/mcp-plan.md](../working-notes/mcp-plan.md))
are where that rule stops fitting. A real context policy:

- **counts tokens with the vendor's tokeniser**, not characters, because the
  bound a model enforces is in tokens and a character count is wrong by a
  factor that depends on the language and on the vendor;
- **orders and collapses tool results**, which are the bulk of a turn with
  tools: what of an old result the model still needs, and in what shape, is
  a judgement about the model;
- **places cache breakpoints**, because a prompt cache is what makes a loop
  of several model calls per turn affordable, and where a breakpoint goes
  and what it costs is a vendor's rule expressed through a framework's API;
- **replays vendor-specific extras** — the signed thinking blocks a vendor
  requires back with a tool result — which only the adapter for that vendor
  can read.

Every one of those is framework- and vendor-specific. A policy above the
port could only be the lowest common denominator of the two engines, and the
placeholder that stood there measured the wrong thing.

## Decision

**The application hands the agent port the full conversation history — the
visible path down to the question being answered — and the tools the run
has. The adapter decides what the model sees.**

- Ordering, trimming and every other kind of context management, and prompt
  caching, are the adapter's, per framework and per vendor.
- Nothing above the port trims: `core.trim_history`, `DEFAULT_HISTORY_CHARS`
  and the `history_chars` setting go, and the "maximum context per agent"
  limit of [operations.md](../specs/operations.md) is a setting an adapter
  reads, when one exists, and not a bound the application applies.
- **One invariant stays above the port, and each adapter's own tests keep it**
  (the shared contract suite has no hook for what the scripted model was
  shown; the two engine test modules each pin it):
  the question being answered is whole in what the model sees. An adapter
  may drop or fold anything before it; it never cuts the turn it is
  answering, because dropping what was just asked answers nothing.
- The paragraph of ADR 0002 that put context management above the port is
  superseded. The rest of ADR 0002 stands: the platform owns the record,
  both engines are stateless per turn, each adapter translates both ways on
  every turn, and no framework persistence is used.
- **ADR 0002 left one question for the day tools came** — whether a
  framework's own persistence would be needed for a run to pause on a tool
  call. Tools came with this decision (2026-09-28) and the answer is no: the
  platform owns the tool loop, calls the tool itself, appends the result to
  the record and runs the engine again from the stored history, so the
  conversation record is the checkpoint ([specs/runs.md](../specs/runs.md))
  and no checkpointer is used.

## Consequences

Good:

- Each adapter can do the right thing for its vendor: count real tokens,
  keep a stable cached prefix across the model calls of one turn, and
  replay what its vendor needs replayed.
- The application has one path for every turn: read the visible path, hand
  it over. A resumed run, a regeneration and the next round of a tool loop
  all look the same from above the port.

Costs:

- **The two engines may send a model different histories from the same
  record**, and a conversation moved from one engine to the other may lose
  something the other adapter would have kept. That is accepted: there is
  no intention to swap engines in the middle of a conversation
  ([working-notes/mcp-plan.md](../working-notes/mcp-plan.md), decision 3).
  The swap test stays as a claim about the record — a conversation started
  on one engine can be continued on the other — and promises nothing about
  what an adapter's policy or a vendor's extras make of it.
- The context policy is one more thing an adapter must be reviewed for, and
  the invariant above is the only part of it the shared suite can check.

## Alternatives considered

- **Keep the policy above the port and grow it**: a token counter per
  vendor, a result-collapsing rule, cache hints in the format. Rejected. It
  would re-implement inside `core` what each framework already exposes
  through its own API, tokeniser and cache controls, and still be unable to
  place a vendor's cache breakpoint or replay its signed blocks.
- **A policy port of its own**, implemented per vendor and called by the
  application. Rejected for now: it is the same code in a different place,
  with a second seam to maintain, and nothing above the port would know
  what to ask it beyond "make this fit". If two adapters ever share a real
  policy, extracting it then is cheaper than designing the seam now.
