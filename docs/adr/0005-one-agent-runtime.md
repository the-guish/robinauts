# ADR 0005 — One agent runtime: the framework runs the turn and keeps the model-facing history; the platform keeps the record

- Status: accepted; supersedes [ADR 0004](0004-context-management-in-the-adapter.md)
  and, in part, [ADR 0002](0002-conversation-persistence.md) (the paragraphs
  named below)
- Date: 2026-09-29

## Context

ADR 0002 decided that the platform owns the conversation record and that
both agent engines are stateless per turn, and it added a requirement the
specs then carried everywhere: any conversation can be continued with any
framework and any vendor, at any time. ADR 0004 moved context management
below the port and left it to each adapter.

A review of the code as it stood on 2026-09-29 found what those two
requirements had produced
([working-notes/framework-runtime-plan.md](../working-notes/framework-runtime-plan.md),
"Where it stood"):

- To keep the record the model's history under two frameworks, the
  application took the tool loop from both. The LangGraph engine became a
  one-node graph over a single streamed model call; the Pydantic AI engine
  ran its graph iterator to the first model request and broke out. Neither
  framework's loop, tool execution, history handling or context mechanisms
  were used.
- Two adapters of about 1,500 lines each carried the same provider-client
  construction, environment scrubbing and answer state machine twice, and
  the requirement that both send byte-identical requests coupled one adapter
  to the other's serializer through a private framework method.
- ADR 0004 put context management in the adapters, and neither implemented
  any: the character trimmer it removed was replaced by nothing.
- Tools over MCP went through a hand-written client, because the SDK's
  dependency tree failed the licence gate on `MIT-0`, a licence more
  permissive than MIT.

The intentions behind bringing the frameworks were the opposite. The
platform carries messages between the interface and the agent and does not
own the tool loop. The framework brings the vendor catalogue. The framework
owns context management, pruning and prompt caching. Nobody intended a
conversation to change framework mid-way; the record exists for people —
the interface, the archive, ex-post analysis — and not for continuing a
conversation on another framework. Two engines were there to prove the seam
framework-agnostic and the component replaceable.

## Decision

**One production agent runtime, on Pydantic AI. The framework runs the
whole turn — the model calls, the tool loop, the tools — and owns the
model-facing history, its context management and its prompt caching. The
platform keeps its own record beside it, projected from what the framework
produced, and stores the framework's transcript per run as opaque data. A
second, deliberately small reference implementation over the vendors' SDKs
keeps the port honest and serves no traffic.**

- **Two representations of one conversation, each with one owner.** The
  native transcript is the framework's, authoritative for what the model
  sees. The record, in the platform's format, is authoritative for people:
  the interface, the archive, analytics, export, deletion. The adapter
  writes the native transcript through the framework and projects each
  completed message into the record. Nothing edits either in place.
- **The native transcript is stored by the platform, per run, unread.**
  Each run keeps the framework's new messages for that turn, serialised by
  the framework's own serializer and tagged with the framework's name and
  version. The next turn's native history is the concatenation of the
  slices of the runs on the visible path, so the tree, edits and
  regenerations stay the platform's: they choose slices rather than rewrite
  them. No framework persistence plugin is used — no checkpointer, no
  session store. The platform's database holds both representations and
  deletes them together.
- **A lossy way back, as a port operation.** Building a native history from
  the record is kept for a run whose slice never landed, for a framework
  version that cannot read an old slice, and for moving an old conversation
  to a new runtime. Vendor-specific parts and compaction summaries are lost
  on that path.
- **A conversation stays on the engine it started on.** Changing an agent's
  engine reaches new conversations only. Changing a conversation's model is
  unchanged: carrying a history to another model of the same runtime is the
  framework's.
- **Context management, pruning and prompt caching are the framework's
  mechanisms**, configured per agent, applied to the native transcript and
  never to the record. Summaries and compaction parts the framework produces
  land in the slice of the run that produced them. Rare, large cuts —
  summarization, server-side compaction — are preferred to sliding windows,
  which break the prompt cache every turn.
- **Tools are the framework's to execute**, through its MCP support, with
  the platform's control kept as hooks: tool names are prefixed per server,
  every call runs under the server's timeout, every call and every result
  is published as an event and recorded as it happens, and a call the run
  must not make yet — an approval, an external result — ends the turn
  waiting through the framework's deferred-tool mechanism, with nothing
  held in memory.
- **The seam is proved at contract size**: by the port, its contract suite
  over a scripted model, a fake runtime for the application's tests, and
  the reference implementation — not by a second production framework.
  Deleting the production runtime must break the configuration and the
  composition table; deleting the reference must break only the contract
  suite's second parametrisation.
- **The MCP Python SDK is adopted.** `MIT-0` joins the allowed list
  ([DEPENDENCIES.md](../../DEPENDENCIES.md)); `pywin32`, Windows-only with
  family-only metadata, is settled by locking for Linux and macOS only
  (framework-runtime-plan.md, step 0).

### What this supersedes

- ADR 0004 as a whole. Context management still belongs to the adapter, but
  through the framework's mechanisms, and the one invariant it kept above
  the port — the question being answered is whole in what the model sees —
  is now the framework's contract with its own history processors.
- Of ADR 0002: "any conversation can be continued with any framework and
  any vendor", and "each adapter translates between the platform's format
  and its framework's format, in both directions, on every turn". What
  stands: the platform owns the record, the record is framework-neutral,
  the runtime is stateless per turn, and no framework persistence plugin is
  used.
- The spec sentences listed in framework-runtime-plan.md, "Spec sentences
  this changes". The specs describe the target from this ADR on; the code
  follows it step by step.

## Consequences

Good:

- What the frameworks were brought for is used: the loop, the vendor
  catalogue, context management and caching, MCP with session reuse and,
  later, OAuth.
- The adapters shrink from two engines and a protocol client to one runtime
  and a projection. The tool loop, the tool port, the request stubs, the
  extras replay, the parity work and their tests go.
- Approvals and suspension arrive through a stateless framework mechanism
  that fits "no framework persistence".

Costs:

- The record no longer says exactly what the model saw. The native slice
  does, and it is stored.
- Native slices are versioned by the framework. A framework upgrade is a
  compatibility question for old conversations, answered by the framework's
  serializer first and by the lossy rebuild second.
- "What was published is what was stored" softens: the record holds what
  the framework returned, the deltas are what the interface showed, and a
  mismatch is a log line rather than a failed run.
- One production framework is a dependency the platform leans on. The
  reference implementation is what bounds the cost of leaving it.

## Alternatives considered

- **Keep both engines and let each run its own loop.** Rejected: every
  feature twice, the port pulled to the lowest common denominator, and two
  approval mechanisms of different shapes — stateless deferred results
  against an interrupt that needs a checkpointer, whose Postgres
  implementation the licence policy excludes.
- **LangChain alone.** Its summarization and prompt-caching middleware are
  the closest to pure configuration, but its human-in-the-loop path needs a
  checkpointer and its meta package is not in the lock. Not chosen; the
  port is designed so that it could be.
- **Drop both frameworks and run the loop over the vendors' SDKs.** Viable
  and smallest in dependencies, but it means owning the loop, the context
  policy and a client per vendor, which are exactly what the frameworks were
  brought not to own. Kept as the shape of the reference implementation.
- **Let the framework persist its own history**, through a checkpointer or
  a session. Rejected: the Postgres checkpointer is licence-excluded, an
  edit or a regeneration would fork framework state rather than choose
  slices, and deletion and retention would have to reach into framework
  tables.
