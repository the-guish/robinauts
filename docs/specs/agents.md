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
- The engine is a property of the agent. Both engines run side by side in
  one deployment. **A conversation stays with its engine**: its memory is
  one framework's, in that framework's own format, and the other cannot
  read it ([ADR 0005](../adr/0005-the-framework-owns-the-loop-and-the-memory.md)).
  Changing an agent's engine in the configuration therefore reaches its
  existing conversations as a **loss of memory**, once: the next turn finds
  a memory written by another engine, does not read it, and begins from
  nothing with the transcript intact and a line in the log. Changing an
  agent's **model** reaches new conversations only: the default was copied
  into each existing one when it started, and what a conversation runs on
  is read off the conversation alone.
- **In this version that means the next turn after a restart.** The
  configuration is read once, at start-up, and the definitions are handed to
  the controller then; a turn looks its agent up afresh, so nothing but a
  reload of the configuration stands between this and the sentence above, and
  a reload is not built. Known limit of the first version, not of the design.

## The agent port

The port is the one
[agent-framework-examples](https://github.com/the-guish/agent-framework-examples)
reached: an adapter is handed a question and the conversation's memory, runs
the whole turn on its framework, and streams what happened in a form no
framework defines.

- The controller knows one port, `Agent`: given the agent's definition, the
  **question** as the person wrote it, the model the run records (the
  conversation's when the run began, never read off the agent) and the
  conversation's **memory** — the state the last finished turn handed back,
  or nothing for a conversation with none — **stream the adapter's events**
  and end with `Done`. Five events, and no more:
  - `TextDelta`: more of the answer's text, as it arrives;
  - `ReasoningDelta`: more of the model's thinking, as it arrives;
  - `ToolCall`: a call the framework is about to make — the vendor's id
    for it, the tool's name and its arguments, whole. An adapter announces
    a call once its arguments are known, since the framework runs it and
    nothing is gained by streaming what the framework will parse anyway;
  - `ToolResult`: what the tool answered, under the call's id and name,
    and whether it is an error;
  - `Done`: the turn is over, with the final answer's text and the memory
    after the turn — the framework's own serialisation of its history, as
    bytes, which the platform stores and never reads.
- **No usage**: what a turn cost is reported in the platform's own terms
  when usage reporting is built, and until then an adapter's events carry
  none and no field is written for one.
- **The framework owns the loop, the context and the memory.** The model
  asks for a tool, the framework calls it, the result goes back, the model
  answers — as many times as the turn needs — and the platform sees it
  happen as events. What the model is sent is the framework's history, kept
  by the framework's own means within the model's window ("A turn" below);
  the system prompt is the agent's, taken from its definition at every turn,
  and is never part of the memory.
- Those events carry no ids of the platform's, no times and no provenance,
  because an adapter has none: it was given a question and a memory. The
  application turns them into the platform's messages and its own run
  events, which is where a message's id, a parent, a run and a row come from
  ([runs.md](runs.md)). **A tool call's id is the vendor's**, chosen by the
  model and carried by the adapter as data: it is stored on the call's part,
  the result names it, and the wire sends it as it is
  ([conversations.md](conversations.md)).
- Both engines are held to the order of their events by the shared contract
  suite: text and reasoning before `Done`; a result answers a call announced
  before it, once, under the name it was announced with; `Done` last and
  once, with every call it announced answered.
- **Streaming is optional; what is streamed is what is kept.** An adapter
  that yields no text delta for an answer may be done with any text — not
  every provider streams. An adapter that yields any is done with exactly
  what it streamed: what a person watched arrive is what is stored, and the
  application keeps the streamed text over `Done`'s.
- An adapter may stream reasoning. This version shows it, keeps it in the
  run's events so that a watcher can re-attach, and puts none of it in a
  message ([conversations.md](conversations.md)).
- **A turn produces at least one answer.** A turn that ends without one is
  a failed run ([runs.md](runs.md)), not a finished turn with nothing in
  it.
- **An adapter reports a failure by raising.** Any exception ends the turn;
  the application records the run `failed` with a description of it, leaves
  the answer that was in flight uncompleted, and stores no memory: the
  conversation resumes from the memory it had. An adapter never yields
  anything after an error.
- **A cancellation is the application cancelling the adapter's task.** An
  adapter must not swallow `CancelledError`: it lets it through, which lets
  the framework's run go, and releases what it holds — an HTTP response, a
  client, a tool session. What was produced before the cancellation stays in
  the transcript, and no memory is stored ([runs.md](runs.md)).
- Two implementations:
  - **LangChain** (the engine is still named `langgraph` in the
    configuration and on every run: the adapter is on LangChain's
    `create_agent`, which is a LangGraph graph). Open source parts only: no
    LangSmith, no LangGraph Platform.
  - **Pydantic AI.**
- Each is confined to its own adapter sub-package, and that is enforced:
  no other code imports the framework, and the two do not import each
  other ([layout.md](../layout.md)). **The discard test:** three places name
  an adapter, and deleting it and its dependencies breaks those and nothing
  else — the import in the composition root and its one entry in the table
  of engines, the import contracts' exceptions for the sub-package, and the
  sub-package's own tests. The composition tests fail too, and name no
  adapter: they say that both engines are wired, which is a claim about the
  table.
- Both must pass one shared contract suite: a streamed answer, an answer
  that was not streamed, a tool round the framework runs, the memory coming
  back and going in, a failure, a cancellation, and holding nothing
  afterwards. The suite hands each adapter its framework's own test model,
  scripted, and tools that are plain functions, exactly as the examples
  hand them over; it asks nothing about the framework.

## A turn

A turn executes as a **run** ([runs.md](runs.md)): a record in the database,
executed in the background, independent of the request that started it. The
controller runs every turn the same way:

1. Load the conversation's messages from the database, take the path down
   to the question being answered, and find the **memory** the turn resumes
   from: the state of the nearest finished run of the same engine on that
   path — for a question, the run that produced the answer it hangs under;
   for a regeneration or an edit, the run before the turn being replaced.
   So a fork of the transcript is a fork of the memory
   ([conversations.md](conversations.md)).
2. Call the agent port with the agent, the run's model, the question's text
   and that memory; publish the events, which the UI watches
   ([wire.md](wire.md)).
3. Turn the events into the platform's messages as they complete — an
   answer with the calls it made, the one tool message of a batch once its
   last result is in, the answer after the results — and append each to
   the conversation.
4. At `Done`, store the memory it carries against the run, in the same
   transaction as the run's ending.
5. The next turn starts again from step 1, with whichever model the
   conversation names.

- **The frameworks keep the context within the window**, each by its own
  means. The LangChain adapter runs `create_agent` with the summarisation
  middleware, which summarises the older history once it passes a share of
  the window and keeps the recent messages as they were — the change is
  saved in the memory, so it is paid for once — and the vendor's cache
  middleware. The Pydantic AI adapter runs an `Agent` with a history
  processor that drops the oldest exchanges — a question and everything up
  to the next one, so that a call is never parted from its result — until
  what is left fits, before every model call and on the memory handed back,
  and asks for the vendor's cache in the model settings. The window each
  measures against is the model's `context_window` when the operator
  configured one, then what the framework knows of the model, then the
  adapter's own default — a gateway's model ids are in no framework's
  table. Neither ever cuts the turn it is answering.
- **The bound on tool rounds is each adapter's.** A model that keeps asking
  for tools cannot run a turn for ever on the operator's account: LangChain's
  recursion limit and Pydantic AI's request limit bound the loop at the
  adapter's own default, and a turn that reaches it fails saying what the
  framework said. The turn's timeout holds over the whole turn, above the
  port.
- Neither framework's persistence is used: no checkpointer, no tables of a
  framework's in the deployment's database. The memory is a column of the
  run ([backend.md](backend.md)), which is what lets one mechanism serve
  both frameworks, a fork of the transcript fork the memory, and a deleted
  conversation take its memory with it.

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
- Both engines report tokens in the platform's terms — input, output,
  model — taken from the provider's response.
- **Nothing phones home.** The engines never enable a framework's hosted
  tracing: LangSmith and Pydantic Logfire stay off whatever the
  environment says. The adapter sets this explicitly, and where a framework
  reads a variable that a switch cannot reach — LangChain's version 1
  tracer, which raises when it is asked for and version 2 is off — the
  adapter unsets the variable rather than leaving a turn to fail over it.
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
  configured (`base_url`, for an `openai-compatible` provider) or the
  vendor's own, named as a constant in the adapter. Left to the client,
  `ANTHROPIC_BASE_URL` and its equivalents would send a turn — and the
  operator's key — to whatever host a stale export named. For the same
  reason the key is always passed and never left to a client's
  `*_API_KEY` fallback, and a vendor-specific proxy variable is pinned off;
  an operator's proxy is `HTTPS_PROXY`, which every outbound call of the
  process obeys.

## Tools

An agent can use tools served by **remote MCP servers** the operator
configured. A model asks for a tool, the framework calls it, the result goes
back to the model, and the model answers — as many times as the turn needs.
The decisions behind this are in
[working-notes/framework-loop-plan.md](../working-notes/framework-loop-plan.md);
the configuration's shape came with
[working-notes/mcp-plan.md](../working-notes/mcp-plan.md).

- **Servers are remote, over Streamable HTTP, and nothing else.** No stdio,
  no sidecars, and the platform runs no MCP server of its own. A server is
  configured the way a model provider is: a `[mcp_servers.<id>]` table with
  its `url` — checked as every configured endpoint is: https, or http on the
  loopback interface, no query, no fragment, no credential in it — the
  **name** of the environment variable its secret is read from, and how the
  secret is sent: `bearer` (the default, `Authorization: Bearer <secret>`),
  `basic` (`Authorization: Basic base64(<user>:<secret>)`, where the table
  also names the user part and the secret is the token), or `none` for a
  public server that takes no credential, which names no variable and is
  sent no header. Nothing about a particular vendor's server is written into
  the platform: GitHub's and Atlassian's remote servers are the two the shape
  was designed against, Microsoft Learn's public one is the third, and all
  three are connected with configuration alone (the sketch below).
- An agent names the servers it may use (`tools`). An agent naming a server
  the deployment has not got is refused at start-up, as one naming an engine
  that is not wired is. The secrets are read at start-up by variable name,
  every missing one reported together, and printed nowhere. Start-up does
  not connect to a server: whether a server takes the credential is found
  out at the first turn of an agent naming it.
- **The frameworks' own MCP clients connect, list and call.** The LangChain
  adapter hands `create_agent` the tools `langchain-mcp-adapters` lists for
  the agent's servers; the Pydantic AI adapter hands its `Agent` one MCP
  toolset per server. Each is built from the server's table — the endpoint,
  the credential as a header, the server's `timeout_seconds` on the
  connection and on every call — and obeys `HTTPS_PROXY` as every outbound
  call of the process does. Neither framework is handed a secret it does not
  send, and neither logs a request.
- **A tool's name on the transcript is the name the framework used for it**,
  which both frameworks spell `<server id>_<tool>` so that two servers
  offering `search` never collide. The platform does not name, sort or bound
  a server's tools: the framework lists them and the model is shown what it
  lists.
- **The results of one call batch are one tool message.** A model may ask
  for several tools in one answer; the framework runs them, the adapter
  yields each result as it lands, and the platform stores one `tool`
  message under the assistant message that made the calls, holding one
  result per call, once the last one is in
  ([conversations.md](conversations.md)).
- **A tool's error is a result, not a failure.** A server answering that a
  call failed, or a call the framework could not validate and sent back to
  the model, becomes a result marked as an error, and the model is told.
  Only a server that cannot be reached at all fails the run. Result content
  is text in this iteration: what the transcript records of a result longer
  than one part may be is its beginning; what the model was sent is the
  framework's, whole.
- **Every call has its own timeout** (`timeout_seconds` on the server, with
  a default), the turn's timeout holds over the whole turn, and each
  adapter's bound on tool rounds holds over the loop ("A turn" above).
- **One identity per deployment.** The secret in the operator's
  configuration means every user's turns act as that principal, and the
  server's audit log names the service account and not the person. Per-user
  credentials (OAuth) are a later iteration
  ([operations.md](operations.md)).
- **Every tool the agent's servers offer runs without asking.** Approval
  before a tool runs is deferred: in this iteration the operator's control
  over what an agent may do is the credential's scopes and the server's own
  admin gates ([runs.md](runs.md)).
- Tool arguments and results are attacker-influenced text going to a model
  and to a browser: bounded on the way in like every part, stored as data,
  rendered as data ([wire.md](wire.md)).

## Details likely to change

- Each engine reaches the providers through its own framework's clients:
  - LangChain: `langchain-openai`, `langchain-anthropic`,
    `langchain-google-genai`, `langchain-aws`;
  - Pydantic AI: `pydantic-ai-slim` with the provider extras needed, not
    the all-inclusive `pydantic-ai`.
  - An OpenAI-compatible endpoint goes through the OpenAI-compatible
    client with a base URL. **OpenRouter does not have to**: it also serves
    Anthropic's Messages API, and an `anthropic-compatible` provider
    reaches it with the Anthropic client both engines already have, which
    is how it is reached in this build (below).
- Every one of these packages passes the licence and vulnerability gates
  at its pinned version, with its transitive tree
  ([open-source.md](open-source.md)) — or is adopted pending the decision
  the "Known findings" below record. A provider whose client fails is not
  offered by that engine until it passes.
- A sketch of the configuration. It is written in the **same file** as
  sign-in ([sign-in.md](sign-in.md)), which is why the model providers are
  `[model_providers.*]` and not `[providers.*]`: that name is already the
  identity providers people sign in with, and one file cannot have a table
  that means one of them here and the other there. `timeout_seconds` (per
  model call), `max_output_tokens` and `context_window` (tokens, what the
  adapters keep the history within) are optional; what they default to is
  the platform's, the engine's and the framework's knowledge of the model
  respectively. `title` is optional too, and is the model's id when it is
  left out.

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
context_window = 200000

[agents.assistant]
title = "Assistant"
model = "sonnet"
engine = "langgraph"
system_prompt = "Play fair."
```

  An `anthropic-compatible` provider is the same thing with the endpoint
  written down. **This is how OpenRouter is reached in this build**: it
  serves Anthropic's Messages API and takes the key in the same
  `x-api-key` header, so both engines reach it with the Anthropic client
  they already have and no OpenAI client is needed. `base_url` is a
  **prefix** the client appends the protocol's own path to, so it stops at
  `/api` and the request goes to
  `https://openrouter.ai/api/v1/messages`; the model names are
  OpenRouter's, `<vendor>/<model>`, which no framework has a table for —
  which is what `context_window` is for:

```toml
[model_providers.openrouter]
kind = "anthropic-compatible"
base_url = "https://openrouter.ai/api"
api_key_env = "ROBINAUTS_OPENROUTER_KEY"

[models.sonnet-via-openrouter]
provider = "openrouter"
name = "anthropic/claude-sonnet-5"
context_window = 200000

[agents.assistant-openrouter]
title = "Assistant (OpenRouter)"
model = "sonnet-via-openrouter"
engine = "pydantic-ai"
```

  The other two kinds are written the same way — here a self-hosted
  gateway speaking OpenAI's protocol, which is a different provider from
  the two above and not another spelling of one. **This build refuses
  them** at start-up, naming the provider, because the client that reaches
  them does not pass the dependency policy (see "Known findings" below);
  the shape is settled all the same, and `base_url` belongs to the two
  protocol kinds and to nothing else:

```toml
[model_providers.gateway]
kind = "openai-compatible"
base_url = "https://gateway.example.com/v1"
api_key_env = "ROBINAUTS_GATEWAY_KEY"
```

  A tool server is a table beside the providers, `[mcp_servers.<id>]`, and
  an agent names the servers it may use in `tools`: the server's `url`;
  `auth`, which is `bearer` unless said otherwise, `basic`, which also
  names the `user` part and takes the token from the variable, or `none`
  for a public server, which names no variable; `secret_env`, the **name**
  of the variable the secret is read from, left out under `none`; and
  `timeout_seconds`, per tool call and optional. The token's scopes, and
  the organisation's own policy on tokens, bound what the server will do;
  nothing here does. Spelt so that an operator connecting GitHub or
  Atlassian copies it and changes the url and the variable name — one
  server with `bearer`, one with `basic`, and one public server with
  `none`:

```toml
[mcp_servers.github]
url = "https://api.githubcopilot.com/mcp/"
secret_env = "ROBINAUTS_GITHUB_TOKEN"

[mcp_servers.jira]
url = "https://mcp.atlassian.com/v2/mcp"
auth = "basic"
user = "robinauts@example.com"
secret_env = "ROBINAUTS_JIRA_TOKEN"
timeout_seconds = 30

[mcp_servers.learn]
url = "https://learn.microsoft.com/api/mcp"
auth = "none"

[agents.assistant-with-tools]
title = "Assistant (tools)"
model = "sonnet"
engine = "langgraph"
tools = ["github", "jira", "learn"]
```

  The agent's `tools` names the servers; the model then sees
  `github_search_repositories`, `jira_search_issues` and
  `learn_microsoft_docs_search`, named by the framework under the server's
  id. Start-up reads the two variables and refuses to start naming every
  one that is unset — the public server names none — and it does not
  connect to any server.

## Known findings

- **The MCP Python SDK (`mcp`), `langchain-mcp-adapters` and `fastmcp` are
  adopted pending a licence decision.** The frameworks' MCP clients are
  built on the SDK, and its tree was known to fail the licence gate:
  `pyjwt[crypto]` brings `cryptography`, which brings `cffi`, whose
  metadata states `MIT-0` — a licence on no list of
  [DEPENDENCIES.md](../../DEPENDENCIES.md) — and `pywin32`, Windows-only,
  states a licence family and no licence (checked 2026-09-28, at 2.2.0).
  The gate is expected red until the decision is taken; what it would take
  is recorded there ("Known exclusions"). The client of our own that stood
  in for the SDK is gone with the loop it served
  ([ADR 0005](../adr/0005-the-framework-owns-the-loop-and-the-memory.md)).
- `langgraph-checkpoint-postgres` depends on `psycopg`, which is
  LGPL-3.0-only. It cannot be adopted as it is, and it is not needed: the
  memory is a column of the platform's own schema, not a checkpointer's
  tables ([ADR 0005](../adr/0005-the-framework-owns-the-loop-and-the-memory.md)).
- `langchain-openai` requires `tiktoken`, which states its licence as the
  licence *text* and no identifier, and which in turn requires `regex`,
  `Apache-2.0 AND CNRI-Python`. Neither resolves under the policy, so the
  client is not adopted and `openai` and `openai-compatible` wait for a
  tree that passes ([DEPENDENCIES.md](../../DEPENDENCIES.md), "Known
  exclusions"). The configuration still names all four kinds — the
  vocabulary is the platform's — and a deployment asking for a kind this
  build cannot reach is refused at start-up, saying so.
- The same tree keeps the same two kinds out of the **Pydantic AI** engine:
  `pydantic-ai-slim[openai]` requires `tiktoken` too.
- So both engines reach **`anthropic` and `anthropic-compatible`**, with
  one client each and nothing else added. OpenRouter is reached as an
  `anthropic-compatible` provider, which is what the exclusion above costs
  and does not cost: a vendor behind an OpenAI-only endpoint is still out
  of reach, and one that also speaks the Messages API is not.
- `langsmith` is a hard dependency of `langchain-core`, and the LangChain
  adapter imports it for **one call**: `langsmith.configure(enabled=False)`,
  made when the engine is constructed, which is the switch langchain-core
  itself consults before the environment. Nothing else in the platform uses
  it, nothing is sent to it, no tracer is ever attached and no client is
  ever built. Because it is imported rather than merely installed, it is a
  direct dependency and is pinned as one.
- **`ANTHROPIC_LOG`**: the Anthropic SDK — which both engines reach the vendor
  through — reads it at import and, on `debug`, writes every request's options
  to standard error, `json_data` included: the system prompt and every message
  of the conversation. Both adapters answer it the same way, in the two halves
  it needs (above): the SDK's loggers are pinned when the engine is built, and
  the variable is removed.
- `logfire-api` arrives with `pydantic-graph`, and `opentelemetry-api` with
  `pydantic-ai-slim`. Neither is imported anywhere in the platform, and both
  are named in the import rule all the same
  ([layout.md](../layout.md)). Pydantic AI has **no environment switch** for
  tracing — it instruments a run only when an agent's `instrument` says so,
  which `logfire.instrument_pydantic_ai()` sets process-wide — so the adapter
  turns it off per agent, where the answer beats the process-wide one, and
  unsets no variable because there is none to unset. It does set the
  framework's `BANNER_ENABLED` to `False`: on its first turn Pydantic AI
  otherwise writes an advertisement for its hosted observability to standard
  error, which is not a thing a server's log is for. One part of this is the
  lock's rather than the code's: `pydantic-graph` opens spans through
  `logfire_api`, which is a no-op shim that **replaces itself with the real
  `logfire`** the moment that package is importable — outside anything the
  per-agent switch reaches. So `logfire` not being in the locked set is part
  of the guarantee, and a test asserts it.
- **A turn that did not end leaves no memory.** A cancelled or failed run
  wrote its calls and results into the transcript and nothing into the
  memory, so the model does not remember it at the next turn. The remedy,
  if a person's cancelled turn turning out to be forgotten by the model
  matters in practice, is a state per step of the loop rather than one at
  the end.
