# ADR 0006 — A third engine, kataloop: our own loop over uni-llm-client

- Status: accepted
- Date: 2026-10-24

## Context

Since [ADR 0005](0005-the-framework-owns-the-loop-and-the-memory.md) an
engine runs the whole turn on its framework: the loop, the context and the
memory. There are two, on LangChain and on Pydantic AI, and the second
exists to keep the seam honest.

Both frameworks bring a great deal we do not use, and decide what we cannot
read without reading their code: how a summary is made, how a tool result is
cut, how a resumed turn finds its place. Each brings tracing that has to be
switched off, and a tree of dependencies to keep under the licence gate.

[kataloop](https://github.com/the-guish/kataloop) is a small agent loop of our
own: it asks a model, runs the tools the model asks for over MCP, gives the
results back, and remembers the conversation. Its contract is robinauts' own
under kataloop's names, so that an adapter is a translation and nothing more,
and its turn, its memory, its tools and its context management are each
written down as a specification with numbered invariants. It reaches the
vendors through [uni-llm-client](https://github.com/the-guish/uni-llm-client),
our own model client, which speaks Anthropic's Messages API and the Chat
Completions API (as its `openrouter` kind) at any endpoint. Neither is
released yet.

## Decision

**kataloop is the third engine, `kataloop`, in
`agent_engines/kataloop_engine`.**

- **A turn runs on a client of its own.** The settings become one
  uni-llm-client provider, under the provider's id: `anthropic` at its own
  endpoint, `anthropic-compatible` as kind `anthropic` at the configured
  `base_url`, and `openai-compatible` as kind `openrouter` at the configured
  `base_url`. The key is asked for at the turn, the client retries
  `MODEL_RETRIES` times, and it is closed when the turn ends. The model is
  `<provider id>/<model name>`, with its timeout, its output limit and its
  window. `openai` is not reachable until uni-llm-client has an OpenAI kind;
  the engine's `kinds()` says so, and the configuration is refused at
  start-up.
- **The memory is kataloop's `MemoryStore`, in the process, whatever the
  storage.** `kataloop[postgres]`, its store over PostgreSQL, is planned and
  not written. The engine holds the store for its lifetime; `setup` makes
  nothing ready and is safe from `robinauts db init`.
- **The tools go through a toolbox of the engine's own, over the MCP SDK.**
  kataloop's `Toolbox` port is what the loop calls a tool through, and
  `kataloop`'s `McpToolbox` is its MCP implementation, on `mcp` 2.3 or later.
  `langchain-mcp-adapters`, the LangChain engine's MCP client, requires `mcp`
  below 2 (0.3.2, its latest release; 0.3.1 states no bound and fails to
  import on 2), and one environment holds one `mcp`. So the engine depends on
  `kataloop-core`, the loop without MCP, and reaches its tool servers through
  a toolbox of its own on the `mcp` 1 SDK the other two engines lock, held to
  kataloop's specification of `McpToolbox`. It becomes kataloop's own once
  `mcp` 2 can be locked here.
- **The dependencies come from git**, pinned to a commit each:
  `kataloop-core` and `uni-llm-client`, both direct, since uv does not follow
  a git dependency's own sources. Both are Apache-2.0 and pass the licence
  gate.

## Consequences

Good:

- A third engine on the same contract suite, with a loop whose every rule
  is written down: what is summarised and when, what a cut result says, which
  rounds a resumed turn repeats. Nothing is sent to a tracing service,
  because there is none.
- The seam is tested against an engine that was written to it, and not
  adapted to it.

Costs:

- **Its memory lives in the server's process.** A restart forgets what every
  kataloop conversation said, and the next turn of each fails: the
  transcript is intact, the engine has no session. A turn the server was
  killed in cannot be resumed, and with two servers on one database, a
  conversation whose next turn the other one takes finds no session either.
  The memory grows with every conversation for as long as the process lives,
  since a deleted conversation is only hidden and no engine is asked to
  forget it. The API test of resumed turns leaves kataloop out for that
  reason. It ends with `kataloop[postgres]`.
- **No OpenAI.** An agent on kataloop needs an Anthropic, Anthropic-compatible
  or OpenAI-compatible provider until uni-llm-client's OpenAI kinds land.
- **The turn being answered is never compacted.** kataloop summarises the
  older exchanges of a conversation and cuts a tool result past a fifth of
  the window, but sends the turn it is answering whole, so a single turn that
  outgrows the window fails at the vendor, where the other two engines go on.
  No prompt cache is placed either: kataloop waits for uni-llm-client to
  place the breakpoints.
- **Git dependencies, until the releases.** CI installs with `uv sync
  --locked --no-build`, which refuses a package that has to be built from
  source, so every job that syncs fails: lint, tests, the browser tests and
  the licence gate. The wheel's job pins the release's requirements by hash,
  which a git requirement cannot carry, and fails on both. The audit leaves
  them out without a word, since the export names them by URL and no
  advisory database knows them. Each release turns a line of
  `[tool.uv.sources]` into a version bound and gives those gates back.
- **A toolbox of our own**, about a hundred lines over the SDK, until
  kataloop's can be locked. Its tests run it against a real MCP server.

## Alternatives considered

- **kataloop's `McpToolbox`, with `mcp` 2.** We could not: the lock resolves
  only by taking `langchain-mcp-adapters` back to 0.3.1, which does not
  import on `mcp` 2, and the LangChain engine would leave the build.
- **Wait for `kataloop[postgres]`.** We did not. The engine keeps the
  contract as it is, its tests run, and the store is one line of its
  factory to change.
- **Wait for the releases.** We did not, for the same reason: a branch that
  is green everywhere but in the gates that refuse a git source is what the
  releases are then checked against.
