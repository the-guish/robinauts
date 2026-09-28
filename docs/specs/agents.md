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
  one deployment. Changing an agent's engine takes effect at the next turn
  of its existing conversations — which is the swap the persistence design
  guarantees. Changing an agent's **model** reaches new conversations only:
  the default was copied into each existing one when it started, and what a
  conversation runs on is read off the conversation alone.
- **In this version that means the next turn after a restart.** The
  configuration is read once, at start-up, and the definitions are handed to
  the controller then; a turn looks its agent up afresh, so nothing but a
  reload of the configuration stands between this and the sentence above, and
  a reload is not built. Known limit of the first version, not of the design.

## The agent port

- The controller knows one port, `Agent`: given the agent's definition, the
  model the run records (the conversation's when the run began, never read
  off the agent), a history and the **tools** the run has, **stream the
  engine's own events** — an answer has begun, more of its text, more of its
  thinking, a tool call announced with its id and name, its arguments as
  they stream, the call complete, the answer is complete and here are its
  parts — and end either "finished" or "waiting on these tool calls"
  ([runs.md](runs.md), "Tools" below). **No usage**: what a turn cost is
  reported in the platform's own terms when usage reporting is built, and
  until then an engine's events carry none and no field is written for one.
- **The history is the full visible path of the conversation, ending in the
  user message being answered** — never trimmed above the port — and the
  system prompt is the agent's and is not one of the messages. So there is no
  second argument for "the new message": the message to answer is the last
  of the history, which is also what a resumed turn, a regenerated one and
  the next round of a tool loop look like, and an engine has one thing to
  translate rather than two. **What of that path the model sees is the
  adapter's to decide** ([ADR 0004](../adr/0004-context-management-in-the-adapter.md)):
  ordering, trimming and other context management, and prompt caching, are
  per framework and per vendor. The one invariant the contract suite keeps
  is that the question being answered is whole in what the model sees.
- **An agent named by a request that this deployment does not have is not
  there**: it is refused exactly as an id that reaches nothing is refused, and
  so is a conversation bound to an agent the operator has since removed.
- Those events carry no ids of the platform's, no times and no provenance,
  because an engine has none: it was given a history and a model. The
  application turns them into the platform's messages and its own turn
  events, which is where a message's id, a parent, a run and a row come from.
  An engine that had to invent one would be deciding something that is not
  its to decide. **A tool call's id is the vendor's**, chosen by the model
  and carried by the engine as data: it is stored on the call's part, the
  result names it, the wire sends it as it is, and the adapter replays it to
  the vendor unchanged ([conversations.md](conversations.md)).
- Both engines are held to the order of their events by the shared contract
  suite: an answer is announced, then streamed, then completed, one at a
  time.
- **Streaming is optional; what is streamed is what is kept.** An engine
  that yields no text delta for an answer may complete it with any text —
  not every provider streams. An engine that yields any must complete with
  exactly what it streamed: what a person watched arrive is what is stored,
  so an engine whose framework rewrites the final message builds its parts
  from what it streamed, or does not stream at all.
- An engine may stream reasoning, and may return it with a finished answer.
  This version shows it, keeps it in the run's events so that a watcher can
  re-attach, and puts none of it in a message
  ([conversations.md](conversations.md)).
- **A turn produces at least one answer.** A turn that ends without one is
  a failed run ([runs.md](runs.md)), not a finished turn with nothing in
  it.
- **An engine reports a failure by raising.** Any exception ends the turn;
  the application records the run `failed` with a description of it and
  leaves the answer that was in flight uncompleted. An engine never yields
  anything after an error.
- **A cancellation is the application cancelling the engine's task.** An
  engine must not swallow `CancelledError`: it lets it through and releases
  what it holds — an HTTP response, a client, a file. What was produced
  before the cancellation stays ([runs.md](runs.md)).
- Two implementations:
  - **LangGraph** (or LangChain). Open source parts only: no LangSmith, no
    LangGraph Platform.
  - **Pydantic AI.**
- Each is confined to its own adapter sub-package, and that is enforced:
  no other code imports the framework, and the two do not import each
  other ([layout.md](../layout.md)). **The discard test:** five places name an
  adapter, and deleting it and its dependencies breaks those and nothing else
  — the import in the composition root and its one entry in the table of
  engines, the import contracts' exceptions for the sub-package, the
  sub-package's own tests, and the shared **swap fixtures**, which exist to
  name both engines at once and cannot be written without both. The
  composition tests fail too, and name no adapter: they say that both engines
  are wired, which is a claim about the table.
- Both must pass one shared contract suite. It includes the swap: a
  conversation started on one engine continues on the other.

## A turn

Both engines are stateless per turn
([ADR 0002](../adr/0002-conversation-persistence.md)). A turn executes as a
**run** ([runs.md](runs.md)): a record in the database, executed in the
background, independent of the request that started it. The controller
runs every turn the same way:

1. Load the conversation's messages from the database, and take the path
   down to the message being answered.
2. Fetch the tools of the servers the agent names, once for the run, and
   call the agent port with the agent, the run's model, that history and
   those tools; publish the events, which the UI watches
   ([wire.md](wire.md)).
3. Translate each new message into the platform's format and append it to
   the conversation as it is produced.
4. If the engine ended waiting on tool calls, call the tools, append their
   results as one tool message, and go back to step 2's call with the
   history read from the store again — which is also what a resumed run
   does, so there is one path. Bounded by `max_tool_rounds` and by the
   turn's timeout ("Tools" below).
5. The next turn starts again from step 1, with whichever engine the agent
   has at that moment and whichever model the conversation names.

- The LangGraph engine compiles its graph without a checkpointer; the
  Pydantic AI engine passes `message_history`. Neither remembers anything
  between turns.
- **Each adapter owns its context policy**: what of the full path it sends,
  in what order, with which cache breakpoints
  ([ADR 0004](../adr/0004-context-management-in-the-adapter.md)). What must
  behave the same under both engines is the record — what is stored, and
  the order of a run's events — and that lives above the port.
- No framework persistence is used, and none is needed for tools: the
  conversation record is the checkpoint (ADR 0002).

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
configured. A model asks for a tool, the platform calls it, the result goes
back to the model, and the model answers — as many times as the turn needs.
The decisions behind this, and their order of work, are in
[working-notes/mcp-plan.md](../working-notes/mcp-plan.md).

- **Servers are remote, over Streamable HTTP, and nothing else.** No stdio,
  no sidecars, and the platform runs no MCP server of its own. A server is
  configured the way a model provider is: a `[mcp_servers.<id>]` table with
  its `url` — checked as every configured endpoint is: https, or http on the
  loopback interface, no query, no fragment, no credential in it — the
  **name** of the environment variable its secret is read from, and how the
  secret is sent: `bearer` (the default, `Authorization: Bearer <secret>`)
  or `basic` (`Authorization: Basic base64(<user>:<secret>)`, where the
  table also names the user part and the secret is the token). Nothing about
  a particular vendor's server is written into the platform: GitHub's and
  Atlassian's remote servers are the two the shape was designed against, and
  both are connected with configuration alone (the sketch below).
- An agent names the servers it may use (`tools`). An agent naming a server
  the deployment has not got is refused at start-up, as one naming an engine
  that is not wired is. The secrets are read at start-up by variable name,
  every missing one reported together, and printed nowhere.
- **The platform owns the tool loop.** The application calls the tool,
  appends the result to the conversation and starts the next engine turn
  from the stored history. Neither framework ever executes a tool, and no
  framework checkpointer is used: an engine yields a tool call as an event
  and its turn ends there, "waiting on these calls". LangGraph's `ToolNode`
  and Pydantic AI's own tool execution and retry prompts stay out. This is
  ADR 0002 kept — the conversation record is the checkpoint
  ([runs.md](runs.md)) — and it answers the question ADR 0002 left open for
  the day tools came ([ADR 0004](../adr/0004-context-management-in-the-adapter.md)).
- **The tool set is fetched once per run and holds for the run.** When a
  turn begins the application asks each server the agent names for its
  tools (`tools/list`), in parallel, and every model call inside that run's
  loop is handed that one list: a stable list for the whole turn, which is
  what a cached prefix wants, and the same list under both engines. There is
  **no cache**: nothing in the process, nothing to size or expire. A server
  that will not list fails the run before the engine is called and before
  any answer is written, naming the server; one that refuses the credential is reported the same way, by name,
  at the first turn of an agent naming it — start-up does not connect to a
  server. A run taken up again after `waiting` lists again, which is the
  ordinary stateless turn.
- **A tool is shown to the model as `<prefix>__<name>`**, where the prefix
  is the server's — written in its table, and defaulting to the server's id
  — so that a call is routed to its server from the name alone and two
  servers offering `search` never collide. Two servers with one prefix are
  refused at start-up, with the rest of the configuration's problems, and so
  is a prefix a name could not be told apart from (one holding `__`, or
  ending in `_`). The vendors bound a tool name at
  64 characters of `[a-zA-Z0-9_-]`, and that is the platform's bound on the
  full name; the prefix is bounded at configuration time so that a real name
  fits after it, and a server's tool whose full name still does not fit is
  left out of that run's list with a line in the log naming the tool — as
  is one whose input schema is not a JSON Schema object at the top, which
  both vendors require of a tool's parameters, and one the server listed
  twice (the second time). The list is sorted by full name, so two engines
  and two runs send byte-identical lists. The run records nothing about its
  tools: the messages already record every call and result by name.
- **The results of one call batch are one tool message.** A model may ask
  for several tools in one answer; the platform runs them in parallel,
  publishes each result as it lands, and stores one `tool` message under the
  assistant message that made the calls, holding one result per call, once
  the last one is in ([conversations.md](conversations.md)).
- **A tool's error is a result, not a failure.** A server answering
  `isError`, or one call timing out, becomes a result marked as an error,
  and the model is told. Only a server that cannot be reached at all fails
  the run. Result content is text in this iteration: a text result is stored
  as it is, and a part of another kind (an image, an embedded resource)
  becomes a text note saying what was left out.
- **Every call has its own timeout** (`timeout_seconds` on the server, with
  a default), the turn's timeout holds over the whole turn, and
  `max_tool_rounds` bounds how many times one turn may go back to the model
  with results; a run that reaches it fails saying so.
- **One identity per deployment.** The secret in the operator's
  configuration means every user's turns act as that principal, and the
  server's audit log names the service account and not the person. Per-user
  credentials (OAuth) are a later iteration
  ([operations.md](operations.md)).
- **Every tool the agent's servers offer runs without asking.** Approval
  before a tool runs is deferred: in this iteration the operator's control
  over what an agent may do is the credential's scopes and the server's own
  admin gates. The MCP annotations a server sends with a tool
  (`readOnlyHint`, `destructiveHint` and the rest) are carried on the
  definition and read by nothing yet; they are what an approval policy would
  read ([runs.md](runs.md)).
- **The engine's share**: bind the definitions to the model; yield a call
  where it used to refuse one — announced with its id and name, its
  arguments as they stream, then complete — and end the turn "waiting on
  these calls", which is the port's word for how one round ended and not the
  run's `waiting` state: the run stays `running` through the loop
  ([runs.md](runs.md)); translate `tool` messages and the two tool parts in
  both directions; and
  carry the vendor's signed reasoning out with the answer and back with the
  history ([conversations.md](conversations.md), "Reasoning").
- Tool arguments and results are attacker-influenced text going to a model
  and to a browser: bounded on the way in like every part, stored as data,
  rendered as data ([wire.md](wire.md)).

## Details likely to change

- Each engine reaches the providers through its own framework's clients:
  - LangGraph: `langchain-openai`, `langchain-anthropic`,
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
  ([open-source.md](open-source.md)). A provider whose client fails is not
  offered by that engine until it passes.
- Not every model has to exist under both engines, but an agent's engine
  can be swapped only if the models its conversations run on do.
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
  OpenRouter's, `<vendor>/<model>`:

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
  `auth`, which is `bearer` unless said otherwise, or `basic`, which also
  names the `user` part and takes the token from the variable; `secret_env`,
  the **name** of the variable the secret is read from; `prefix`, what the
  server's tools are shown to the model under, the server's id when left
  out; and `timeout_seconds`, per tool call and optional. The token's
  scopes, and the organisation's own policy on tokens, bound what the server
  will do; nothing here does. Spelt so that an operator connecting GitHub or
  Atlassian copies it and changes the url and the variable name — one server
  with `bearer`, one with `basic`:

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

[agents.assistant-with-tools]
title = "Assistant (tools)"
model = "sonnet"
engine = "langgraph"
tools = ["github", "jira"]
```

  The agent's `tools` names the servers; the model then sees
  `github__search_repositories` and `atlassian__search_issues`, and a
  call is routed to its server by the name alone. Start-up reads both
  variables and refuses to start naming every one that is unset; it does
  not connect to either server ([runs.md](runs.md), "Tools").

## Known findings

- **The MCP Python SDK (`mcp`) is not adopted**: its tree fails the licence
  gate. `pyjwt[crypto]` brings `cryptography`, which brings `cffi`, whose
  metadata states `MIT-0` -- a licence on no list of
  [DEPENDENCIES.md](../../DEPENDENCIES.md) -- and `pywin32`, Windows-only,
  states a licence family and no licence (checked 2026-09-28, at 2.2.0). The
  plan named the fallback for this case
  ([working-notes/mcp-plan.md](../working-notes/mcp-plan.md), step 2): the
  MCP adapter is a client of our own over `httpx` for the three calls a
  client needs -- `initialize`, `tools/list`, `tools/call` -- over Streamable
  HTTP, with session ids, protocol-version negotiation and an SSE response
  read by hand. The import rule confining the SDK to `adapters/tools/mcp/` is
  written all the same, before the fact ([layout.md](../layout.md)).

- `langgraph-checkpoint-postgres` depends on `psycopg`, which is
  LGPL-3.0-only. It cannot be adopted as it is (ADR 0002). The LangGraph
  core is not affected.
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
  one client each and nothing else added, and the swap holds for every
  model either of them has. OpenRouter is reached as an
  `anthropic-compatible` provider, which is what the exclusion above costs
  and does not cost: a vendor behind an OpenAI-only endpoint is still out
  of reach, and one that also speaks the Messages API is not.
- `langsmith` is a hard dependency of `langchain-core`, and the LangGraph
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
