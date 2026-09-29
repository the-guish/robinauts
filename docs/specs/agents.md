# Agents, engines and models

## Agents

- The operator defines named **agents** in the configuration: a name, a
  system prompt, a **default** model, the engine that runs it, and the tool
  servers it may use ("Tools" below).
- A user chooses an agent when starting a conversation.
- Users do not create agents. That is planned.
- **The model is the conversation's.** When a conversation starts it takes
  its agent's default, unless its author picks another of the models the
  operator configured, and the author may change it at any point after. A
  change takes effect at the next turn: a turn already running keeps the
  model it started with, and every answer records the model that produced
  it ([conversations.md](conversations.md)).
- **A model the deployment no longer offers refuses the turn**, with an
  error of its own that says so, and its author moves the conversation to
  another model; which model it was goes to the log. It is not answered as
  "not found", the way an agent the operator has removed is: model ids are
  no secret, and the person is looking at the conversation. There is no
  falling back to the agent's default: the point of choosing is knowing who
  answers.
- The engine is a property of the agent, and **a conversation is bound to
  the engine it started on** ([ADR 0005](../adr/0005-one-agent-runtime.md)):
  the framework's transcript of the conversation is the framework's, and no
  other runtime reads it. Changing an agent's engine reaches new
  conversations only, as changing its **model** does: the default was copied
  into each existing one when it started, and what a conversation runs on is
  read off the conversation alone.
- **In this version that means the next turn after a restart.** The
  configuration is read once, at start-up, and the definitions are handed to
  the controller then; a turn looks its agent up afresh, so nothing but a
  reload of the configuration stands between this and the sentence above, and
  a reload is not built. Known limit of the first version, not of the design.

## The agent port

- The controller knows one port, the **agent runtime**: given the agent's
  definition, the model the run records (the conversation's when the run
  began, never read off the agent), the **native history** of the visible
  path — the framework's own transcript, concatenated from the slices of the
  runs on that path, or nothing for a conversation's first turn — the
  **input** of the turn — the question being answered, or, for a run taken
  up again, the tool results or approvals it was waiting on — and the tool
  servers the agent names, **run the whole turn** and stream the runtime's
  own events: an answer has begun, more of its text, more of its thinking, a
  tool call announced with its id, its name and its arguments, the tool's
  result as it lands, the answer is complete and here are its parts; then
  the turn is over, **finished** or **waiting** on calls the framework did
  not run, and here is the framework's transcript of the turn
  ([runs.md](runs.md), "Tools" below). **No usage**: what a turn cost is
  reported in the platform's own terms when usage reporting is built.
- **The framework runs the loop.** The runtime calls the model, executes the
  tools the model asks for through the framework's MCP support, feeds the
  results back and calls the model again, as many times as the turn needs
  within the bounds the agent's configuration sets. The application does not
  run a tool and does not run the engine again from the record: it records
  what the runtime streams, message by message, and stores the transcript
  the runtime hands back at the end
  ([ADR 0005](../adr/0005-one-agent-runtime.md)).
- **What the model sees is the runtime's to decide**, through the
  framework's own mechanisms — ordering, trimming, summarization, server-side
  compaction and prompt caching — configured per agent and applied to the
  native history alone. The record is never compacted. That the question
  being answered is whole in what the model sees is the framework's contract
  with its own history processors, and the runtime's own tests keep it.
- **The runtime is stateless per turn.** It is handed the native history and
  hands back the turn's slice; nothing is remembered between calls in the
  process, and no framework persistence plugin is used
  ([ADR 0002](../adr/0002-conversation-persistence.md),
  [conversations.md](conversations.md), "The native transcript").
- **Those events carry no ids of the platform's, no times and no
  provenance**, because a runtime has none. The application turns them into
  the platform's messages and its own turn events, which is where a message's
  id, a parent, a run and a row come from. **A tool call's id is the
  vendor's**, chosen by the model and carried by the runtime as data.
- **An agent named by a request that this deployment does not have is not
  there**: refused exactly as an id that reaches nothing is refused, and so
  is a conversation bound to an agent the operator has since removed.
- The runtime is held to the order of its events by the contract suite: an
  answer is announced, then streamed, then completed; a call is announced,
  then completed, then answered; one at a time.
- **Streaming is optional; what is streamed is what is kept.** A runtime that
  yields no text delta for an answer may complete it with any text. One that
  yields any completes with what it streamed, or the mismatch is logged and
  the completed text is what is stored: the record holds what the framework
  returned.
- **A runtime reports a failure by raising.** Any exception ends the turn;
  the application records the run `failed` with a description of it and
  leaves the answer that was in flight uncompleted. A runtime never yields
  anything after an error.
- **A cancellation is the application cancelling the runtime's task.** A
  runtime must not swallow `CancelledError`: it lets it through and releases
  what it holds — an HTTP response, a client, an MCP session.
- **Two implementations.** The production runtime is on **Pydantic AI**,
  used as it is meant to be used: its agent, its toolsets and MCP support,
  its event stream, its history processors and its cache settings. The
  **reference runtime** is a deliberately small implementation over the
  vendors' own SDKs, bounded by the contract suite: it exists so that the
  port stays framework-agnostic and the component replaceable, serves no
  traffic, and is offered by no configuration. Each is confined to its own
  adapter sub-package, and that is enforced ([layout.md](../layout.md)).
  **The discard test:** deleting the production runtime and its
  dependencies breaks the configuration, the composition table and its own
  tests; deleting the reference breaks only the contract suite's second
  parametrisation.
- A conversation moved to another runtime is rebuilt from the record,
  losing what only the native transcript held
  ([conversations.md](conversations.md)); it is not a continuation and
  nothing promises it.

## A turn

The runtime is stateless per turn
([ADR 0002](../adr/0002-conversation-persistence.md)). A turn executes as a
**run** ([runs.md](runs.md)): a record in the database, executed in the
background, independent of the request that started it. The controller
runs every turn the same way:

1. Load the conversation from the database: the visible path down to the
   message being answered, and the native slices of the runs on it.
2. Call the runtime with the agent, the run's model, that native history,
   the turn's input and the tool servers the agent names; publish the
   events, which the UI watches ([wire.md](wire.md)).
3. Project each message the runtime completes — an answer with the calls it
   made, the tool message holding the results — into the platform's format
   and append it to the record as it is produced.
4. When the turn ends, store the framework's transcript of it as the run's
   slice. A turn that ends waiting — on an approval, on an external result —
   leaves the run `waiting` with its slice stored, and is taken up again
   from step 1 with what it waited for as the input.
5. The next turn starts again from step 1, on the conversation's engine and
   whichever model the conversation names.

- Nothing above the port trims, summarizes, caches or runs a tool.
- What must behave the same whatever the runtime is the record — what is
  stored, and the order of a run's events — and that lives above the port.
- No framework persistence plugin is used, and none is needed for tools or
  for waiting: the run's slices along the path are the checkpoint, and the
  record is the archive and the lossy fallback
  ([ADR 0005](../adr/0005-one-agent-runtime.md)).

## Model providers

- Goal 5: OpenAI, Anthropic, Gemini, OpenRouter, AWS Bedrock; no vendor
  privileged.
- Keys are the operator's. The configuration gives the *name* of an
  environment variable per provider (for Bedrock, the standard AWS
  credential chain). Keys are read at start-up, never stored in the
  database, never logged, never sent to the browser. Users do not supply
  keys.
- The operator declares providers and models; agents and conversations
  refer to a model by the platform's own id for it. The configuration is the
  platform's, not either framework's.
- A model may have a `title`, the name a person picks it by, as an agent
  has; left out, it is the model's id.
- A provider's `kind` names either a **vendor** — `anthropic`, `openai` —
  or a **protocol at an address the operator gives**:
  `anthropic-compatible` is an endpoint that speaks Anthropic's Messages
  API, `openai-compatible` one that speaks OpenAI's. The protocol kinds
  are the ones that carry a `base_url`, and the only ones: a vendor has one
  endpoint, the engine pins it, and a second answer to "where is it" would
  be a way to send the operator's key somewhere else.
- The runtime reports tokens in the platform's terms — input, output, model — taken from the provider's response.
- **Nothing phones home.** The runtime never enables the framework's
  hosted tracing: Pydantic Logfire stays off whatever the environment says,
  set per agent where the framework's own switch is, and the framework's
  banner for it is off too.
- **And nothing is written down.** A log of this platform never carries the
  content of a conversation. A vendor SDK's own debug logging does, and is
  switched on by an environment variable it reads when it is *imported*, so an
  adapter that only removed the variable would be too late: the adapter pins
  the vendor loggers that write request bodies — the one that emits the record
  as well as its parent, since a level set on a child is what a logger decides
  by — at a level where no request is ever a record, and removes the variable
  as well so that a subprocess does not start again from the beginning. Each
  logger's **own** level is set and not merely its effective one, or an
  operator turning their root logger up afterwards would turn the vendor's
  logging on with it. What this does not do is fight a logger an operator
  names at debug in their own configuration after start-up: that is their
  deliberate act on their own machine.
- **A vendor's endpoint is not taken from the environment either.** Every
  client is built with the endpoint it is to use: the one the operator
  configured (`base_url`, for the two protocol kinds) or the
  vendor's own, named as a constant in the adapter. Left to the client,
  `ANTHROPIC_BASE_URL` and its equivalents would send a turn — and the
  operator's key — to whatever host a stale export named. For the same
  reason the key is always passed and never left to a client's
  `*_API_KEY` fallback, and a vendor-specific proxy variable is pinned off;
  an operator's proxy is `HTTPS_PROXY`, which every outbound call of the
  process obeys. What a client reads that no argument can override — headers
  it merges in (`ANTHROPIC_CUSTOM_HEADERS`, `OPENAI_CUSTOM_HEADERS`), and the
  OpenAI client's organisation, project and admin key, which it reads
  whenever the argument is left out and leaves out only when it finds nothing
  — is taken out of the environment when the engine is built, and the key is
  pinned as its own header as well, where the caller's value wins. There is
  no way to configure an OpenAI organisation or project in this version: a
  key belongs to one project already, and a header nobody wrote in the
  configuration is not sent.
- **The OpenAI kinds speak Chat Completions**, both of them: Pydantic AI's
  `OpenAIChatModel` rather than its Responses model. An `openai-compatible`
  endpoint — a gateway, vLLM, OpenRouter — speaks Chat Completions, and one
  protocol for both kinds keeps them symmetric. The Responses API is a
  later decision (see "Known findings" for what it would buy).

## Tools

An agent can use tools served by **remote MCP servers** the operator
configured. A model asks for a tool, the framework calls it, the result goes
back to the model, and the model answers — as many times as the turn needs.
The platform records every call and every result as it happens, and shows
them. The decisions are in [ADR 0005](../adr/0005-one-agent-runtime.md) and
[working-notes/framework-runtime-plan.md](../working-notes/framework-runtime-plan.md);
the shape of the configuration came from the MCP work before it
([working-notes/mcp-plan.md](../working-notes/mcp-plan.md)).

- **Servers are remote, over Streamable HTTP, and nothing else.** No stdio,
  no sidecars, and the platform runs no MCP server of its own. A server is
  configured the way a model provider is: a `[mcp_servers.<id>]` table with
  its `url` — https, or http on the loopback interface, no query, no
  fragment, no credential in it — the **name** of the environment variable
  its secret is read from, and how the secret is sent: `bearer` (the
  default), `basic` (with the `user` part in the table), or `none` for a
  public server. Nothing about a particular vendor's server is written into
  the platform.
- An agent names the servers it may use (`tools`). An agent naming a server
  the deployment has not got is refused at start-up. The secrets are read at
  start-up by variable name, every missing one reported together, and
  printed nowhere.
- **The framework talks to the servers**, through its MCP support: one
  session per server for the run, the tool list fetched when the run
  begins, the calls made inside the framework's loop. The platform holds no
  MCP client. The MCP Python SDK is the framework's dependency and is
  admitted by the licence policy ([DEPENDENCIES.md](../../DEPENDENCIES.md)).
- **What the platform keeps of a call is a hook, not the loop.** The
  runtime wraps every tool execution so that: the call runs under the
  server's `timeout_seconds`, and one that runs out becomes an error result
  the model is told; a tool is shown to the model as `<prefix>__<name>`
  through the framework's prefixing, the prefix being the server's (its id
  unless the table says otherwise), so that two servers offering `search`
  never collide; every call and every result is published as an event the
  moment it happens and recorded in the conversation; and, later, the
  credential for the call is the person's rather than the deployment's.
- **A tool's error is a result, not a failure.** A server answering that the
  call failed, or a call that ran out of its time, is a result marked as an
  error, and the model is told. A server that cannot be reached at all, or
  that will not list its tools when the run begins, fails the run, naming
  the server.
- **The results of one call batch are one tool message** in the record, one
  result per call, written when the last one is in
  ([conversations.md](conversations.md)); each result is published as it
  lands. Result content is text in this version: a part of another kind
  becomes a text note saying what was left out.
- **The bounds are the agent's configuration**, read by the runtime: how
  many times one turn may go back to the model (`max_tool_rounds`, a
  default when left out), and the turn's timeout, which holds over the
  whole turn. A run that reaches the bound fails saying so.
- **One identity per deployment.** The secret in the operator's
  configuration means every user's turns act as that principal, and the
  server's audit log names the service account and not the person.
  Per-user credentials (OAuth) are a later iteration
  ([operations.md](operations.md)).
- **Every tool the agent's servers offer runs without asking.** Approval
  before a tool runs is the framework's deferred-tool mechanism, still to be
  wired: a call marked as needing approval ends the turn **waiting**, with
  the run's slice stored and nothing held in memory, and the run is taken up
  again with the approval as its input ([runs.md](runs.md)). The MCP
  annotations a server sends with a tool (`readOnlyHint`,
  `destructiveHint` and the rest) are what that policy reads.
- Tool arguments and results are attacker-influenced text going to a model
  and to a browser: bounded on the way into the record like every part,
  stored as data, rendered as data ([wire.md](wire.md)).

## Details likely to change

- The runtime reaches the providers through the framework's clients:
  `pydantic-ai-slim` with the provider extras needed (`anthropic` and
  `openai` today; `google` and `bedrock` when those kinds arrive), not the
  all-inclusive `pydantic-ai`. The reference runtime uses the two vendor
  SDKs directly.
  - An OpenAI-compatible endpoint goes through the OpenAI client with a
    base URL, and an Anthropic-compatible one through the Anthropic client.
    **OpenRouter serves both protocols**, so it may be configured as either
    kind (below); the two are two providers, with a model list each, and not
    two spellings of one.
- Every one of these packages passes the licence and vulnerability gates
  at its pinned version, with its transitive tree
  ([open-source.md](open-source.md)). A provider whose client is not in the
  build -- because it fails the gates, or because it has not been adopted --
  is not offered by that engine.
- A conversation stays on its runtime, so which models a runtime reaches
  is a question for new conversations only.
- A sketch of the configuration. It is written in the **same file** as
  sign-in ([sign-in.md](sign-in.md)), which is why the model providers are
  `[model_providers.*]` and not `[providers.*]`: that name is already the
  identity providers people sign in with, and one file cannot have a table
  that means one of them here and the other there. `timeout_seconds` (per
  model call) and `max_output_tokens` are optional; what they default to is
  the platform's and the engine's business respectively. `title` is optional
  too, and is the model's id when it is left out.

```toml
[model_providers.anthropic]
kind = "anthropic"
api_key_env = "ROBINAUTS_ANTHROPIC_KEY"

[models.sonnet]
provider = "anthropic"
name = "claude-sonnet-5"
title = "Claude Sonnet 5"
timeout_seconds = 120
max_output_tokens = 8192

[agents.assistant]
title = "Assistant"
model = "sonnet"
engine = "pydantic-ai"
system_prompt = "Play fair."
```

  An `anthropic-compatible` provider is the same thing with the endpoint
  written down. **This is one way OpenRouter is reached**: it serves
  Anthropic's Messages API and takes the key in the same `x-api-key`
  header, so the runtime reaches it with the Anthropic client. `base_url` is
  a **prefix** the client appends the protocol's own path to, and
  Anthropic's client appends `/v1/messages`, so it stops at `/api` and the
  request goes to `https://openrouter.ai/api/v1/messages`; the model names
  are OpenRouter's, `<vendor>/<model>`:

```toml
[model_providers.openrouter]
kind = "anthropic-compatible"
base_url = "https://openrouter.ai/api"
api_key_env = "ROBINAUTS_OPENROUTER_KEY"

[models.sonnet-via-openrouter]
provider = "openrouter"
name = "anthropic/claude-sonnet-5"

[agents.assistant-openrouter]
title = "Assistant (OpenRouter)"
model = "sonnet-via-openrouter"
engine = "pydantic-ai"
```

  The OpenAI kinds are written the same way: `openai` is OpenAI itself,
  with no `base_url`, and `openai-compatible` any endpoint that speaks
  OpenAI's Chat Completions — here a self-hosted gateway, which is a
  different provider from the ones above and not another spelling of one.
  `base_url` belongs to the two protocol kinds and to nothing else, and it
  is again a **prefix**, but of a different path: OpenAI's client appends
  `/chat/completions` alone, so the API's version is part of what the
  operator writes. OpenAI's own endpoint, which the engines pin, is
  `https://api.openai.com/v1`; a gateway at
  `https://gateway.example.com/v1` is sent its requests at
  `https://gateway.example.com/v1/chat/completions`; and OpenRouter over
  this protocol is `base_url = "https://openrouter.ai/api/v1"`, reached at
  `https://openrouter.ai/api/v1/chat/completions` — one `/v1` more than the
  `anthropic-compatible` spelling above, because that client writes the
  version itself. The key travels as `Authorization: Bearer <key>`:

```toml
[model_providers.openai]
kind = "openai"
api_key_env = "ROBINAUTS_OPENAI_KEY"

[model_providers.gateway]
kind = "openai-compatible"
base_url = "https://gateway.example.com/v1"
api_key_env = "ROBINAUTS_GATEWAY_KEY"

[models.gpt]
provider = "openai"
name = "gpt-5.5"
title = "GPT-5.5"

[models.scout-on-the-gateway]
provider = "gateway"
name = "meta-llama/Llama-4-Scout-17B-16E-Instruct"

[agents.assistant-gpt]
title = "Assistant (GPT)"
model = "gpt"
engine = "pydantic-ai"
```

  `max_output_tokens` means the same thing under every kind; what an
  engine sends when it is left out differs by protocol. Anthropic's API
  requires a ceiling on every request, so over it the engines send 8192;
  Chat Completions requires none, and over it the engines send none, since
  on OpenAI's reasoning models the ceiling covers the reasoning as well as
  the answer and a number chosen to bound an answer can be spent entirely
  on thinking. A configured one is sent in the field the kind takes: to
  `openai` as `max_completion_tokens`, the field OpenAI's current models
  take (its reasoning models refuse the older one), and to
  `openai-compatible` as `max_tokens`, the field OpenRouter and older
  compatible servers know and some know alone — Pydantic AI's own
  OpenRouter profile makes the same choice. The reference runtime sends the same.

  A tool server is a table beside the providers, `[mcp_servers.<id>]`, and
  an agent names the servers it may use in `tools`: the server's `url`;
  `auth`, which is `bearer` unless said otherwise, `basic`, which also
  names the `user` part and takes the token from the variable, or `none`
  for a public server, which names no variable; `secret_env`, the **name**
  of the variable the secret is read from, left out under `none`; `prefix`,
  what the server's tools are shown to the model under, the server's id
  when left out; and `timeout_seconds`, per tool call and optional. The
  token's scopes, and the organisation's own policy on tokens, bound what
  the server will do; nothing here does. Spelt so that an operator
  connecting GitHub or Atlassian copies it and changes the url and the
  variable name — one server with `bearer`, one with `basic`, and one
  public server with `none`:

```toml
[mcp_servers.github]
url = "https://api.githubcopilot.com/mcp/"
secret_env = "ROBINAUTS_GITHUB_TOKEN"

[mcp_servers.jira]
url = "https://mcp.atlassian.com/v2/mcp"
auth = "basic"
user = "robinauts@example.com"
secret_env = "ROBINAUTS_JIRA_TOKEN"
prefix = "atlassian"
timeout_seconds = 30

[mcp_servers.learn]
url = "https://learn.microsoft.com/api/mcp"
auth = "none"

[agents.assistant-with-tools]
title = "Assistant (tools)"
model = "sonnet"
engine = "pydantic-ai"
tools = ["github", "jira", "learn"]
```

  The agent's `tools` names the servers; the model then sees
  `github__search_repositories`, `atlassian__search_issues` and
  `learn__microsoft_docs_search`, and a call is routed to its server by the
  name alone. Start-up reads the two variables and refuses to start naming
  every one that is unset — the public server names none — and it does not
  connect to any server ([runs.md](runs.md), "Tools").

## Known findings

- **The MCP Python SDK is adopted**
  ([ADR 0005](../adr/0005-one-agent-runtime.md)). Its tree failed the
  licence gate on `cffi`, which states `MIT-0`, and on `pywin32`,
  Windows-only with family-only metadata. `MIT-0` is on the allowed list
  since 2026-09-29 ([DEPENDENCIES.md](../../DEPENDENCIES.md)); `pywin32` is
  settled by locking for Linux and macOS only
  ([working-notes/framework-runtime-plan.md](../working-notes/framework-runtime-plan.md),
  step 0). The hand-written client that stood in for it goes with the
  refactor.
- `langgraph-checkpoint-postgres` depends on `psycopg`, which is
  LGPL-3.0-only ([ADR 0002](../adr/0002-conversation-persistence.md)).
  Moot once the LangGraph engine is removed; recorded because the licence
  fact stands for any future checkpointer.
- `tiktoken` states its licence as the licence *text* and no identifier,
  and requires `regex`, `Apache-2.0 AND CNRI-Python`. Both are settled —
  CNRI-Python is on the allowed list and `tiktoken` 0.14.0 is excepted by
  name — and both leave with `langchain-openai`, which is what brought them.
  **Nothing on a turn's path asks `tiktoken` for anything**: Pydantic AI
  reaches for it only in its embeddings, which are not used. Token counting
  for the context policy uses the vendors' counting endpoints through the
  framework, never a local tokenizer.
- **The OpenAI kinds speak Chat Completions** through Pydantic AI's
  `OpenAIChatModel`. What that leaves out, for the day the Responses API is
  decided on: OpenAI's signed reasoning, which only the Responses API
  returns, and, on the newest models, function tools with reasoning, which
  OpenAI's documentation routes to the Responses API. The runtime sends no
  `reasoning_effort`, so on such a model a turn with tools may be refused by
  the vendor while a turn without tools answers.
- **Reasoning a compatible endpoint streams in a field of its own**
  (`reasoning`, `reasoning_content`) is read by the framework as thinking
  and streamed as reasoning; it is unsigned and is not kept in the record.
- **`ANTHROPIC_LOG`** and the vendor SDKs' request-body logging: the
  runtime pins the SDKs' loggers below the level at which a request is a
  record, and removes the variable, when it is built ("Model providers").
- `logfire-api` arrives with `pydantic-graph` and `opentelemetry-api` with
  `pydantic-ai-slim`. Neither is imported by the platform, and both are
  named in the import rule ([layout.md](../layout.md)). Pydantic AI
  instruments a run only when its agent's `instrument` says so; the runtime
  sets it off per agent and sets the framework's `BANNER_ENABLED` to
  `False`. `pydantic-graph` opens spans through `logfire_api`, a no-op shim
  that replaces itself with the real `logfire` when that package is
  importable, so `logfire` not being in the locked set is part of the
  guarantee, and a test asserts it.
- **Findings about the two engines' parity** — byte-identical Chat
  Completions requests, the differences left between them, the shapes of a
  streamed call each took or refused — are superseded by ADR 0005 and leave
  with the LangGraph engine. What still matters is the runtime's own
  behaviour, pinned by its tests.
