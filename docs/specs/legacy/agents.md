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
  read it ([ADR 0005](../../adr/0005-the-framework-owns-the-loop-and-the-memory.md)).
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
  other ([layout.md](../../layout.md)). **The discard test:** three places name
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
   ([wire.md](../wire.md)).
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
- **The OpenAI kinds speak Chat Completions**, both of them and under both
  engines: `ChatOpenAI` with the Responses API switched off, and Pydantic
  AI's `OpenAIChatModel` rather than its Responses model. An
  `openai-compatible` endpoint — a gateway, vLLM, OpenRouter — speaks Chat
  Completions, and one protocol for both kinds keeps the two kinds and the
  two engines symmetric. The Responses API is a later decision (see "Known
  findings" for what it would buy).

## Tools

An agent can use tools served by **remote MCP servers** the operator
configured. A model asks for a tool, the framework calls it, the result goes
back to the model, and the model answers — as many times as the turn needs.
The decisions behind this are in
[working-notes/framework-loop-plan.md](../../working-notes/framework-loop-plan.md);
the configuration's shape came with
[working-notes/mcp-plan.md](../../working-notes/mcp-plan.md).

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
  ([operations.md](../operations.md)).
- **Every tool the agent's servers offer runs without asking.** Approval
  before a tool runs is deferred: in this iteration the operator's control
  over what an agent may do is the credential's scopes and the server's own
  admin gates ([runs.md](runs.md)).
- Tool arguments and results are attacker-influenced text going to a model
  and to a browser: bounded on the way in like every part, stored as data,
  rendered as data ([wire.md](../wire.md)).

## Details likely to change

- Each engine reaches the providers through its own framework's clients:
  - LangChain: `langchain-openai`, `langchain-anthropic`,
    `langchain-google-genai`, `langchain-aws`;
  - Pydantic AI: `pydantic-ai-slim` with the provider extras needed, not
    the all-inclusive `pydantic-ai`.
  - An OpenAI-compatible endpoint goes through the OpenAI client with a
    base URL, and an Anthropic-compatible one through the Anthropic client.
    **OpenRouter serves both protocols**, so it may be configured as either
    kind (below); the two are two providers, with a model list each, and not
    two spellings of one.
- Every one of these packages passes the licence and vulnerability gates
  at its pinned version, with its transitive tree
  ([open-source.md](../open-source.md)) — or is adopted pending the decision
  the "Known findings" below record. A provider whose client fails is not
  offered by that engine until it passes.
- A sketch of the configuration. It is written in the **same file** as
  sign-in ([sign-in.md](../sign-in.md)), which is why the model providers are
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
  written down. **This is one way OpenRouter is reached**: it serves
  Anthropic's Messages API and takes the key in the same `x-api-key`
  header, so both engines reach it with the Anthropic client. `base_url` is
  a **prefix** the client appends the protocol's own path to, and
  Anthropic's client appends `/v1/messages`, so it stops at `/api` and the
  request goes to `https://openrouter.ai/api/v1/messages`; the model names
  are OpenRouter's, `<vendor>/<model>`, which no framework has a table for —
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
context_window = 128000

[agents.assistant-gpt]
title = "Assistant (GPT)"
model = "gpt"
engine = "langgraph"
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
  OpenRouter profile makes the same choice. Both engines send the same.

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
  adopted** (2026-09-30). The frameworks' MCP clients are built on the
  SDK, and its tree was known to fail the licence gate on two packages.
  `pyjwt[crypto]` brings `cryptography`, which brings `cffi`, whose
  metadata states `MIT-0`; the identifier joined the allowed list of
  [DEPENDENCIES.md](../../../DEPENDENCIES.md) by the owner's decision.
  `pywin32`, which `mcp` needs on Windows alone, states a licence family
  and no licence, and the wheel read for an exception carries an LGPL-2.1
  package (`adodbapi`), which no exception may cover; the lock is resolved
  for Linux and macOS only since, and Windows is not a target. Both are
  recorded there ("Known exclusions"; checked at `mcp` 1.30.0). The client of our own that stood
  in for the SDK is gone with the loop it served
  ([ADR 0005](../../adr/0005-the-framework-owns-the-loop-and-the-memory.md)).
- `langgraph-checkpoint-postgres` depends on `psycopg`, which is
  LGPL-3.0-only. It cannot be adopted as it is, and it is not needed: the
  memory is a column of the platform's own schema, not a checkpointer's
  tables ([ADR 0005](../../adr/0005-the-framework-owns-the-loop-and-the-memory.md)).
- `langchain-openai` requires `tiktoken`, which states its licence as the
  licence *text* and no identifier, and which in turn requires `regex`,
  `Apache-2.0 AND CNRI-Python`. Neither resolved under the policy. Both are
  settled now -- CNRI-Python is on the allowed list, and `tiktoken` 0.14.0 is
  excepted by name for its licence text (its package licence only: the BPE
  tokenizer files it fetches at runtime are assets, a separate question).
  **Nothing on a turn's path asks `tiktoken` for anything**: `ChatOpenAI`
  reaches for it only to count tokens (`get_num_tokens` and its relatives),
  which neither engine calls -- the summarising middleware measures
  approximately, and the Pydantic AI adapter's history processor by
  characters ("A turn") -- and Pydantic AI only in its embeddings, which are
  not used; so no tokenizer file is fetched and the asset question does not
  arise. A turn is held to that under each engine by one test run under
  both
  (`test_an_openai_turn_streams_its_text_and_sends_the_configuration_s_request`,
  in `tests/unit/test_engines_over_chat_completions.py`), which replaces
  `tiktoken`'s `get_encoding` and `encoding_for_model` with functions that
  fail the test, so a warm tokenizer cache, which closed sockets would not
  notice, cannot hide a call.
- So **both engines reach all four kinds**: `anthropic` and
  `anthropic-compatible` through the Anthropic client, `openai` and
  `openai-compatible` through the OpenAI one (`langchain-openai` under
  LangChain, `pydantic-ai-slim[openai]` under Pydantic AI). The
  configuration names the same four kinds, and a kind no engine of a build
  reaches is still refused at start-up, saying so; in this build there is
  none.
- **Chat Completions, and what it costs.** Both OpenAI kinds speak Chat
  Completions under both engines ("Model providers" above). What that
  leaves out, for the day the Responses API is decided on: OpenAI's signed
  reasoning, which only the Responses API returns — so the memory of a
  conversation over Chat Completions holds no reasoning of the model's to
  replay — and OpenAI's own server-side tools, which the platform does not
  use. And **tools with reasoning**, on the newest models. OpenAI's model
  pages say, for GPT-6 Sol and GPT-6 Luna (checked 2026-09-28), that Chat
  Completions "supports function calling only with `reasoning_effort` set
  to `none`", and send GPT-6 Astra's tools to the Responses API. For GPT-5.6
  Sol, an OpenAI Support reply on OpenAI's developer forum
  (community.openai.com, post 1386454, 2026-09-07) quotes the refusal:
  "Function tools with reasoning_effort are not supported for gpt-5.6-sol
  in /v1/chat/completions. To use function tools, use /v1/responses or set
  reasoning_effort to 'none'." GPT-5.6 Luna and Terra reason by default
  (at `medium`) and are untried. The engines send no `reasoning_effort`, so
  on any of these a turn with tools may be refused by the vendor, and a turn
  without tools answers. The libraries pinned here know GPT-5.6 Sol, Luna
  and Terra and GPT-6 Astra by name, and not GPT-6 Sol or Luna, which are
  newer; nothing more than the sources above is claimed for any of them. The
  demo's OpenAI models are GPT-5.5 and the GPT-5.4 family, which carry no
  such note and whose smaller models do not reason unless asked
  (`demo/README.md`).
- **The two engines do not send one request for one conversation**, and are
  not held to: each keeps a memory of its own, in its own format, and writes
  the request from it as its framework does
  ([ADR 0005](../../adr/0005-the-framework-owns-the-loop-and-the-memory.md)).
  What is held alike, by one test run under both engines over OpenAI's
  protocol, is where a request goes, what signs it, what of the
  configuration it carries — the model, the timeout, the ceiling in the
  field the kind takes — and what a turn makes of what comes back: the
  text, a tool round the framework runs, a refusal.
- **A `<think>` in the text is text under LangChain and thinking under
  Pydantic AI.** `langchain-openai` has no rule for it, so what the vendor
  wrote is the answer, streamed and stored with its tags; Pydantic AI lifts
  a streamed `<think>` delta, and what follows it up to `</think>`, out of
  the answer and into thinking, which the adapter streams as reasoning. Each
  framework's own reading, left as it is.
- **Reasoning a compatible endpoint streams in a field of its own is shown by
  one engine only.** Some `openai-compatible` servers stream a model's
  reasoning beside the text, in a field that is not OpenAI's: `reasoning`
  (gpt-oss through Ollama or OpenRouter) or `reasoning_content` (DeepSeek,
  Moonshot, vLLM). Pydantic AI reads either as thinking, and the Pydantic AI
  engine streams it as reasoning; `langchain-openai` reads neither, and the
  LangGraph engine shows nothing and does not fail.
- **A streamed call whose name is sent again on every delta.** Some
  compatible servers repeat the id and the name on every delta of a call,
  and both frameworks append every name they are sent. The Pydantic AI
  adapter reads the stream through a class of its own that takes a name
  sent again as the same name, so the framework runs the tool it has; under
  LangChain the doubled name reaches the framework's tool node, which tells
  the model there is no such tool, and the model answers to that — a
  difference recorded here, with a test on the Pydantic AI side, and not
  fixed on the other.
- **OpenAI's `refusal` field is not read.** Chat Completions carries a
  separate `refusal` in place of the content only for Structured Outputs,
  which the platform never asks for; a model declining a question otherwise
  says so in its text, which is streamed and stored like any other.
- **`ChatOpenAI` is handed clients the adapter built**, where
  `ChatAnthropic` is handed arguments. Left to build its own,
  `langchain-openai` takes its HTTP client from a cache shared by every turn
  of the process and gives it TCP socket options read from
  `LANGCHAIN_OPENAI_TCP_*`; the adapter's own clients are built per turn,
  as the other client and the other engine's are, with exactly what the
  configuration says. Every other default it would take from the
  environment — `OPENAI_PROXY`, `LC_OUTPUT_VERSION`, its own stream timeout,
  whether to ask for the stream's usage — is passed as an argument.
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
  the variable is removed. `OPENAI_LOG` is read the same way by the OpenAI
  SDK, which writes no request body at debug in this version and is pinned
  all the same.
- `logfire-api` arrives with `pydantic-graph`, and `opentelemetry-api` with
  `pydantic-ai-slim`. Neither is imported anywhere in the platform, and both
  are named in the import rule all the same
  ([layout.md](../../layout.md)). Pydantic AI has **no environment switch** for
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
