# Plan: tools over MCP

Written 2026-09-28, replacing the notes of 2026-09-27 that closed the POC
session. Progress against it is in [mcp-progress.md](mcp-progress.md). Not a
spec: the decisions that are settled, the one deferred,
what the work costs across the codebase, and the order to build it in. The
wanted behaviour is in [specs/runs.md](../specs/runs.md),
[specs/agents.md](../specs/agents.md) and
[specs/conversations.md](../specs/conversations.md) ("Tools", planned); the
spec sentences this plan changes are listed at the end.

## What is wanted

- An agent can use tools served by remote MCP servers the operator
  configured. A model asks for a tool, the platform calls it, the result goes
  back to the model, and the model answers -- as many times as the turn needs.
- Every call and every result is a message of the conversation, persisted as
  it is produced and shown as it arrives, on either engine, and a run with
  tools survives a dropped request like every other run.
- An operator connects a server the way they connect a model provider: a
  table in the TOML, a secret by environment-variable name, and an agent that
  names the servers it may use. Nothing about a particular vendor's server is
  written into the platform.

## Settled

Four decisions, made at the start of the session that wrote this plan. Where
a spec or the code still says otherwise, this is the direction and the spec
changes (listed at the end).

1. **The platform owns the tool loop.** The application calls the tool,
   appends the result to the conversation and starts the next engine turn
   from the stored history. Neither framework ever executes a tool, and no
   framework checkpointer is used. LangGraph's `ToolNode` and Pydantic AI's
   own tool execution and retry prompts stay out; an engine yields a tool
   call as an event and its turn ends there. This is ADR 0002's "the
   conversation record is the checkpoint", kept.

2. **The application hands the agent port the full conversation history --
   the visible path -- and the tools; the adapter decides what the model
   sees.** Ordering, trimming and other context management, and prompt
   caching are the adapter's, per framework and per vendor. Today the
   history is trimmed above the port (`core.trim_history`, called from
   `Turns._history`, with `DEFAULT_HISTORY_CHARS`); that goes, and with it
   the sentence in ADR 0002 and the specs that context management is the
   first example of what must live above the port. **This needs an ADR**
   (0004), since it reverses a recorded decision: the reason is that a real
   context policy counts tokens with the vendor's tokeniser, orders and
   collapses tool results, and places cache breakpoints, all of which are
   framework- and vendor-specific, and a policy above the port could only be
   the lowest common denominator of two.

3. **There is no intention to swap engines in the middle of a conversation.**
   Models may change (the conversation's model is the user's to change), and
   losing context when the model changes is acceptable. The contract suite's
   swap test stays as a claim about the record -- a conversation started on
   one engine can be continued on the other -- and is not extended to promise
   that vendor-specific extras survive a swap.

4. **MCP servers are remote, over Streamable HTTP, and nothing else.** No
   stdio, no sidecars, and the project runs no MCP server of its own. A
   server is configured in the operator's TOML like a model provider, its
   secret by environment-variable name, and an agent names the servers it may
   use.

5. **The tool set is fetched once per run and holds for the run.** Settled
   2026-09-28, after the four above. When a turn begins, the application
   asks each server the agent names for its tools (`tools/list`), and every
   model call inside that run's loop is handed that one list: the model sees
   a stable tool list for the whole turn, which is what a cached prefix
   wants, and both engines are handed the same list. A run taken up again
   after `waiting` lists again, which is the ordinary stateless turn. There
   is **no cache**: no state in the process, nothing to size or expire, and a
   server that will not list fails the run before anything is written, with
   the server named. The cost accepted is one round trip per server per
   turn, in parallel across servers, before the first model call. A cache in
   front of the port's listing is the named extension (see "Backlog"), and it
   is an adapter's change that nothing above the port would see.

6. **The list is named by a prefix, sorted by name, and not recorded on the
   run.** Settled 2026-09-28, with decision 5.
   - **Naming.** A tool is shown to the model as `<prefix>__<name>`, where
     the prefix is the server's, written in its table and defaulting to the
     server's id. A call is routed to its server from the name alone, and
     two servers offering `search` never collide. The vendors bound a tool
     name -- Anthropic at 64 characters matching `^[a-zA-Z0-9_-]{1,64}$`, and
     OpenAI the same length -- and that is the platform's bound on the full
     name (`MAX_TOOL_NAME_CHARS`). The prefix is bounded at configuration
     time so that a real tool name fits after it; a server's tool whose full
     name still does not fit is **left out of that run's list with a line
     in the log naming the tool**, because the names are only known when the
     run lists them (decision 5) and neither failing the turn over one long
     name nor sending a name the vendor will refuse is an answer.
   - **Order.** Sorted by full name. Free, and it makes two engines and two
     runs send byte-identical lists, which is what a cached prefix wants.
   - **Record.** The run records nothing about its tools: the messages
     already record every call and result by name, and a hash nobody reads
     would be a column for its own sake. `runs` does not change. Recording
     the definitions themselves is in the backlog for the day analytics or a
     replay wants to know what the model saw.

7. **The results of one call batch are one tool message.** Settled
   2026-09-28. A model may ask for several tools in one answer; the platform
   runs them in parallel and stores **one** `tool` message under the
   assistant message that made the calls, holding one `ToolResultPart` per
   call, each naming the call it answers. The tree rule stands as it is: a
   tool message hangs under the assistant message, the next assistant
   message hangs under the tool message, and the visible path holds every
   result. Sibling tool messages would have read as one replacing the other
   (ADR 0003), and a chain of them would have been an order the parallel
   calls do not have. It is also the shape the vendors' APIs want back: every
   result of a batch in one message. What follows: a result is matched to
   its call by call id inside the message; while a batch is running, each
   result is published as it lands and the message is written when the last
   one is in, which is the same rule as an answer's deltas today; a run
   cancelled in the middle of a batch leaves the calls without a result
   message, which the format already allows.

8. **Signed reasoning is stored in the assistant message's `extras`, keyed
   by vendor.** Settled 2026-09-28. On the Anthropic models both engines
   reach, thinking is on unless turned off, and an answer that makes a tool
   call carries signed thinking blocks that must be replayed unchanged when
   the results go back. By decision 1 the next engine call is built from the
   stored history, so the blocks have to be in it. They go in the reserved
   `extras` of the assistant message under the vendor's key
   (`extras.anthropic`), which is exactly what `docs/specs/conversations.md`
   reserved that key for: the engine returns them with `AnswerCompleted`,
   the application stores them unread and bounded (the 64 KiB rule), and the
   adapter that reaches that vendor replays them; the other adapter, and
   every other reader, reads past them. **No reasoning text enters a
   message**: what is stored is the vendor's opaque blocks, and the
   reasoning a person watched arrive stays in the run's events as today.
   The blocks are bound to the model that made them, so a conversation
   moved to another model loses them and nothing else (decision 3); on the
   models whose blocks are also bound to their prefix, the adapter asks the
   API to drop a block that no longer matches rather than refuse the
   request, since an operator editing an agent's prompt between turns must
   not break its conversations. Turning thinking off when tools are bound
   was the alternative, and it is not allowed on the newest models.
   **Corrected in step 1's review (2026-09-28)**: the vendor's block carries
   the text of the thinking with its signature, so "no reasoning text enters
   a message" is not a promise the platform can make. What it promises is
   that the blocks are **never read as reasoning** -- stored unread, never
   rendered, in no Markdown export, sent to no other vendor -- and a JSON
   export writes the document whole (`specs/conversations.md`).

9. **Four defaults**, settled 2026-09-28 with the above, each small enough
   to change later without a decision on paper.
   - **A bound on tool rounds per turn.** `Turns` takes `max_tool_rounds`,
     generous by default; a run that reaches it fails saying so. The
     ten-minute turn timeout stays over the whole turn.
   - **Result content is text in this iteration.** A text result is stored
     as it is; a part of another kind (an image, an embedded resource)
     becomes a text note saying what was left out, and joins the format
     when user messages carry images too.
   - **Tool messages are ordinary messages everywhere else**: on the visible
     path, shown to project members and share links, in both exports,
     deleted with the conversation. Nothing is special-cased.
   - **A tool's error is a result, not a failure.** A server answering
     `isError`, or one call timing out, becomes a result with `is_error` and
     the model is told. Only a server that cannot be reached at all fails
     the run.

## Design targets

Two servers the design must let an operator connect **with configuration
alone**. Nothing in the tree names either; they are what the configuration
shape and the error messages are tested against in review.

- **GitHub's remote MCP server**: `https://api.githubcopilot.com/mcp`, and on
  GitHub Enterprise `https://copilot-api.<subdomain>.ghe.com/mcp`. A
  fine-grained or classic personal access token goes in
  `Authorization: Bearer <token>`; the token's scopes and the organisation's
  PAT policy bound what the server will do.
- **Atlassian's remote MCP server** (Jira, Confluence, JSM, Bitbucket):
  `https://mcp.atlassian.com/v2/mcp`. Token authentication is enabled by the
  organisation's admin; a service-account key goes in
  `Authorization: Bearer <key>`, a personal API token in
  `Authorization: Basic base64(<email>:<token>)`.

What they decide about the design:

- **The endpoint is per server** (`url`), checked as every configured
  endpoint is (`domain.is_endpoint_url`: https, or http on the loopback
  interface, no query, no fragment, no credential in it). The Enterprise
  subdomain is why a fixed catalogue of vendors would not do.
- **How the secret is sent is configuration, not an assumption.** Two
  schemes cover both targets: `bearer` (the default) and `basic`, where the
  configuration also names the user part and the secret is the token. A
  vendor-specific header value (Sentry's `Sentry-Bearer`) is a third scheme
  to add later, not now.
- **A server that refuses the credential is reported by name.** Start-up
  does not connect to a server (decision 5), so the refusal an admin-gated
  feature answers with is met at the first turn of an agent naming it: the
  run fails before anything is written, and the log says that server `<id>`
  refused the credential, never that a turn failed for no stated reason. A
  start-up probe that lists every configured server and reports refusals
  before anyone asks a question is in the backlog.
- **One identity per deployment.** A token in the operator's TOML means
  every user's turns act as that principal, and the server's audit log names
  the service account and not the person. That is acceptable for this
  iteration and is said in the spec; per-user OAuth (which Notion's hosted
  server requires, and which GitHub and Atlassian also offer) is a later
  iteration with a persistence question of its own, and is out of scope
  here. **And every tool the agent's servers offer runs without asking**:
  approval before a tool runs is deferred ("Backlog"), so in this iteration
  the operator's control over what an agent may do is the token's scopes,
  and Atlassian's admin gate. The spec says that too.

## Open

The questions this plan opened with were settled as decisions 5 to 9, except
**approval before a tool runs**, which was **deferred** rather than decided:
it is in the backlog with what the deferral costs, and step 7 is where it
would be built. A step that meets a question this plan does not answer adds
it here rather than deciding it in passing.

- **Signed blocks that do not fit `extras`** (added in step 1's review,
  2026-09-28). `extras` is bounded at 64 KiB written out, and a vendor's
  thinking blocks carry the thinking's text, so a long-thinking tool turn
  can produce more than fits. Refusing the message fails every such turn;
  dropping the blocks breaks the replay the vendor requires. Not decided:
  step 4's adapters meet it first, and the choice -- a larger bound for this
  key alone, dropping what does not fit and asking the vendor to drop what it
  can no longer match, or asking the vendor for the shorter display of its
  thinking -- is made there, on what the vendor's API allows, and recorded
  here.

## The seams, by layer

Everything a tool touches goes through the two events an engine ends a turn
with today and the one place the application reads a history. The rest is
carrying a call out and a result back.

- **Domain** (`domain/conversation.py`, `domain/turn.py`, `domain/agents.py`,
  a new `domain/tools.py`): `ToolCallPart` (call id, tool name, arguments as
  a JSON-able mapping) and `ToolResultPart` (call id, text, an `is_error`
  flag) behind `SUPPORTED_PART_KINDS`, a tool message holding one result per
  call of its parent (decision 7); `Role.TOOL` in `SUPPORTED_ROLES`;
  `AnswerCompleted.extras`, the vendor-keyed mapping of decision 8, carried
  on to `Message.extras`; `ToolDefinition` (namespaced name, description, input
  schema, the MCP annotations) and `ToolServerConfig` (id, url, auth scheme,
  the secret's variable name, the user part for `basic`, the tool-name
  prefix defaulting to the id, timeout) and `MAX_TOOL_NAME_CHARS`;
  `AgentDefinition.tools: tuple[str, ...]` naming servers; new engine
  events (below); `kept_parts` learns which of the new parts an answer keeps.
  A tool's arguments and a result's content are bounded like every part
  (`MAX_PART_CHARS`, and the 64 KiB rule where a mapping is stored).

- **Core** (`core/conversation_format.py`, `core/conversation_tree.py`,
  `core/runs.py`, `core/models_config.py`, a new `core/tools.py`): the
  encoding of the two new parts (adding a kind does not move the version);
  the tree already allows `tool` under `assistant` and `assistant` under
  `tool` and does not change (decision 7); a new check that a tool message
  answers exactly the calls of its parent, once each, refused where a stored
  conversation is read as every other fault of the tree is;
  `check_engine_events` and `check_event_order` grow the tool events and the
  new way a turn may end; `parse_models_config` reads `[mcp_servers.*]` and
  the agents' `tools`, refusing an unknown server, a duplicate, a bad
  endpoint and a missing variable name with every problem at once, as the
  provider tables are, the prefix bounded so that a tool name fits after
  it; naming, sorting and leaving out a name that does not fit (decision 6)
  is a pure function here, over the lists the servers answered. `trim_history` leaves core: what
  replaces it is per adapter (below).

- **Ports** (`ports/agents.py`, a new `ports/tools.py`): `run_turn(agent,
  history, tools, *, model)` -- the full visible path, never trimmed above
  the port, and the `ToolDefinition`s the run has. A new port,
  `ToolServers`: list the tools of a server, call one with arguments under a
  timeout, answer with the platform's result parts. It is one port over
  every configured server, keyed by server id, because the application never
  holds a client. Its docstring says what the vendor clients' do: no
  environment fallbacks, nothing phones home, no arguments or results in a
  log.

- **Application** (`application/turns.py`): `_history` hands over
  `tree.visible_path()` ending at the question (`path_to(run.message_id)`)
  with no trimming and no `history_chars`; `_produce` grows the loop of
  decision 1: a tool-call event completes an **assistant** message holding
  `ToolCallPart`s (and the vendor's blocks in its `extras`, decision 8), the
  calls are executed through `ToolServers` in parallel, each result is
  published as it lands and the batch completes **one tool** message when
  the last one is in (decision 7), and the engine is run again from the
  history read back from the store -- which is what a resumed run does too,
  so there is one path. The turn's timeout stays over the whole of it,
  every tool call has its own, and `max_tool_rounds` bounds the loop
  (decision 9). A tool that fails answers a result with `is_error` and the
  model is told; a server that cannot be reached at all fails the run. A
  cancellation in the middle of a batch leaves the calls without a result
  message, which the format already allows.

- **Adapters** (`adapters/agents/langgraph/engine.py`,
  `adapters/agents/pydantic_ai/engine.py`, a new `adapters/tools/mcp/`):
  both engines translate `tool` messages and the two new parts in both
  directions, bind the tool definitions to the model, and **yield** a tool
  call where they raise `NO_TOOLS` today -- the three detections in
  LangGraph (`tool_call_chunks`, a `tool_call` block, a `non_standard`
  `tool_use`) and the `BaseToolCallPart` / `ToolCallPartDelta` branches in
  Pydantic AI are exactly the lines. LangGraph binds with `bind_tools` and
  keeps its one node (no `ToolNode`); Pydantic AI keeps breaking after the
  first model request node, which is already what stops the framework
  running a tool, and declares the tools as ones the framework does not
  execute. Each adapter reads the vendor's signed thinking blocks off the
  framework's message and returns them in `AnswerCompleted.extras`, and puts
  them back on the assistant message when it translates a history; each
  asks the API to drop a block it can no longer match rather than refuse the
  request (decision 8). Each adapter owns its **context policy**: what of
  the full path it sends, in what order, with which cache breakpoints. The
  MCP adapter is the
  one `ToolServers` implementation, built from the config records and the
  secrets read at start-up, one client per server, pinned as the vendor
  clients are (endpoint from the configuration, credential as a header, no
  retries the application cannot see, `HTTPS_PROXY` obeyed and nothing
  vendor-specific).

- **Datastore** (`datastore/schema.sql`, `datastore/conversations.py`): a
  message with tool parts is a document like any other and needs nothing,
  and the run records nothing about its tools (decision 6). **The schema
  does not change in this iteration.** If a later step adds a column all
  the same, that is `SCHEMA_SHA256` re-pinned, `SCHEMA_VERSION` still 1 and
  a local database recreated (CONTRIBUTING.md, "Changing the database
  schema").

- **API** (`api/agui.py`, `api/schemas.py`): `AguiMapper` maps a tool-call
  message to `TOOL_CALL_START` / `TOOL_CALL_ARGS` / `TOOL_CALL_END` and each
  result, as it lands, to `TOOL_CALL_RESULT`, under the call's own id -- the
  vendor's, stored on the part, which is how a result and a loaded
  conversation name one call one way (corrected in step 1's review: not
  derived from the message id and the position as the reasoning brackets
  are) -- the SSE id on the last derived event, and the same re-attach
  no-ops (a `*_START` for an open call, a `*_END` for a closed one). `sent_role` learns `tool`. The messages a
  conversation is served with carry the new parts; the OpenAPI snapshot
  changes and is regenerated with `scripts/update-openapi.sh`.

- **Frontend** (`chat/assistant-ui/agui/events.ts`,
  `chat/assistant-ui/state.ts`, `chat/assistant-ui/runtime.tsx`): decode the
  four tool events (unknown types are ignored today, so an older UI keeps
  working); `ChatPart` gains `tool-call` and `tool-result`; the reducer
  attaches a call to the running assistant message and a result to its
  call; the runtime hands `tool-call` parts to the vendored `tool-fallback`
  and `tool-group`, which were kept in step 20 for this. **Arguments and
  results are rendered as data**: text, never Markdown-with-HTML, never a URL
  turned into a link without the CSP in mind.

- **Configuration and composition** (`adapters/config_file.py`, `app.py`,
  `cli.py`): `check_api_keys` gains the servers' secrets, read by variable
  name and printed nowhere; the composition root builds the MCP adapter
  beside the engines and hands `Turns` the `ToolServers`; `Turns` refuses an
  agent naming a server the deployment has not got, at start-up, as it
  refuses an engine it has not wired. The demo's TOML gains a commented
  server table.

## Order of work

One stacked branch per step, reviewed and committed one at a time, as the
POC and the model selection were
([three-agent recipe](poc-progress.md)). Each step leaves every check green,
Postgres included. Steps 1 and 2 have no code that reaches a server; step 5
is the first turn that calls one.

1. **Decisions on paper.** ADR 0004: context management belongs to the
   adapter, superseding that part of ADR 0002; the spec sentences below; the
   settled decisions above written into `agents.md`, `runs.md` and
   `conversations.md` as behaviour rather than plans, and the deferral of
   approval said where it costs something. Tests: none. Review: the specs
   read as one document.

2. **The dependency.** The MCP Python SDK (`mcp`, MIT, 2.2.0 at the time of
   writing) through the licence gate with its whole tree: `mcp-types`,
   `jsonschema`, `pyjwt[crypto]`, `sse-starlette`, `python-multipart`,
   `opentelemetry-api`, `httpx2`, `anyio`, `starlette`, `uvicorn` -- most
   already in the lock through FastAPI and the vendor SDK. A pull request of
   its own, per CONTRIBUTING.md, saying why: JSON-RPC over Streamable HTTP
   with session ids, protocol-version negotiation and SSE responses is a
   protocol, and the lesson of step 17 was to prefer the real tool's parser
   over a growing one of ours. The import contract confines it to
   `adapters/tools/mcp/`, and `opentelemetry` stays named in the Pydantic AI
   rule: the SDK may import it, nothing of ours does. If the tree fails the
   gate, the fallback is a client of our own over `httpx` for the three
   calls a client needs (`initialize`, `tools/list`, `tools/call`), and this
   step says so rather than excepting a licence.

3. **The format.** `ToolCallPart`, `ToolResultPart`, `Role.TOOL` supported,
   their encoding, one tool message per call batch and the check that it
   answers its parent's calls (decision 7), `Message.extras` carried
   through, `sent_role`.
   Tests: `test_conversation_domain.py`, `test_conversation_format.py`,
   `test_conversation_tree.py`, `test_stored_reads.py`, `test_agui.py` for
   the role. Nothing produces these messages yet; a stored one round-trips
   through both stores (`contracts/conversation_store.py`).

4. **The port and the engines.** `run_turn(agent, history, tools, *,
   model)` with the untrimmed path; `DEFAULT_HISTORY_CHARS`, `history_chars`
   and `core.trim_history` removed; each adapter given a context policy of
   its own with the one invariant the contract suite keeps -- the question
   being answered is whole in what the model sees; the new engine events
   (a call announced with its id and name, its arguments as they stream, the
   call complete; the turn ending "waiting on these calls") in
   `check_engine_events`; both engines yielding a call where they raise
   `NO_TOOLS`, translating `tool` messages and the new parts; the vendor's
   signed thinking blocks out through `AnswerCompleted.extras` and back on
   to the assistant message (decision 8). The fake engine and
   the contract suite grow a scripted tool call. Tests:
   `test_langgraph_engine.py`, `test_pydantic_ai_engine.py`,
   `test_engine_swap.py`, `contracts/agents.py`, `test_fake_agent.py`,
   `test_conversation_history.py` becomes each adapter's. **Until step 5 the
   application still fails a turn that asks for a tool**, now on the event
   rather than on the raise, so nothing half-answered is stored.

5. **Configuration and the tool loop.** `[mcp_servers.*]` and `tools` on an
   agent in the parser and the records; secrets checked at start-up; the
   `ToolServers` port, its MCP adapter and an in-memory fake; `Turns` given
   the port, the loop in `_produce` with its bound (decision 9), the tool
   list fetched once per run and named, sorted and bounded (decisions 5 and
   6), the turn events for a tool-call message, for each result as it lands
   and for the one tool message of a batch (decision 7) through `_Stream` with
   `check_event_order` holding the order. Tests:
   `test_models_config.py`, `test_app_composition.py`, a new
   `test_mcp_adapter.py` over a scripted Streamable HTTP server in the test
   (as `tests/integration/test_api_sign_in.py` stands in for a sign-in
   provider), `test_turn_lifecycle.py` for the loop, the timeouts, a failing
   tool, a server that is gone, and a cancellation mid-call. **First live turn**: an opt-in test
   under `tests/live/` against Microsoft Learn's server
   (`https://learn.microsoft.com/api/mcp`), which needs no credential, beside
   the vendor live tests.

6. **Wire and interface.** The four AG-UI tool events in `AguiMapper` with
   their ids and the re-attach rules; `events.ts`, the reducer, the runtime
   handing parts to `tool-fallback`; the conversation loaded with tool parts
   in it. Tests: `test_agui.py`, `test_stream_routes.py`, `events.test.ts`,
   `state.test.ts`, `runtime.test.tsx`, `Chat.test.tsx`; the OpenAPI
   snapshot. Review reads three places first (the notes' lesson): refusals
   that reflect input, re-attach no-ops, and anything rendered from a tool's
   text.

7. **Suspending on a tool.** The `waiting` state for a call whose result does
   not come inside the run -- a person's approval, an external job: the run
   ends `waiting` with the call stored and no result, a route appends the
   result, and the run is resumed as the ordinary stateless turn. The sweep
   already leaves `waiting` alone. **Deferred**: not in this iteration
   ("Backlog"); the loop of step 5 is written all the same so that "the
   result arrives later" is the same code path as "the result arrives now",
   and a batch with one result missing is what a suspended run looks like.

8. **Demo and the operator's page.** `demo/robinauts.toml.in` with a
   commented `[mcp_servers.*]` table; `agents.md`'s configuration sketch
   showing a server with `bearer` and one with `basic`, spelt so that an
   operator connecting GitHub or Atlassian copies it and changes the url and
   the variable name; `operations.md` on what a tool server sees (one
   identity per deployment).

## Backlog

Ideas noted so that they are not lost. Nothing here is decided or committed
to; each is taken up, or dropped, when something measured or wanted calls
for it.

- **Approval before a tool runs.** Deferred on 2026-09-28. What it would be:
  a policy per server saying which tools need a person (MCP's
  `readOnlyHint` and `destructiveHint` annotations are what it would read,
  and an operator allowlist of tool names per server is the smallest form
  of it), the run suspending `waiting` with the calls stored and a result
  missing, a route that appends the approval or the refusal as that result,
  and the affordance the vendored `tool-fallback` already has. What the
  deferral costs: in this iteration every tool an agent's servers offer runs
  without asking, under the deployment's one identity, and the operator's
  control is the token's scopes and the server's own admin gates; the spec
  says so ("Design targets"). Step 7 is where it would be built.
- **A bounded cache in front of `tools/list`**, inside the MCP adapter. The
  application would still ask once per run and still get a list that holds
  for the run (decision 5); a run inside the bound would make no request.
  Rebuildable state, so it passes the layout's test for what a process may
  keep. Worth taking up if the per-turn round trip is measured to matter.
  What it is not: listing at start-up with change notifications, which needs
  a long-lived session per server and is a different shape.
- **Per-user credentials to a tool server** (OAuth), so that a server's
  audit log names the person and not the deployment. A persistence question
  of its own ("Design targets").
- **The tool definitions recorded on the run** (decision 6 records
  nothing), once analytics or a replay wants to know what the model saw.
- **A start-up probe of the configured servers**: list each one once and
  report a refused credential or an unreachable endpoint by name before the
  first turn meets it. Optional and off by default, since a deployment must
  start while a server is down.

## Spec sentences to change

- [specs/agents.md](../specs/agents.md): "The agent port" -- the history is
  the full visible path, **not** trimmed, and the port is handed the tools;
  "A turn" -- step 2 loses "trimmed", and "Anything that must behave the same
  under both engines lives above the port. Fitting a long history into a
  context window is the first example" is replaced by the adapter owning its
  context policy (ADR 0004); "Tools" -- from planned to how it works: remote
  Streamable HTTP servers, `[mcp_servers.*]`, an agent's `tools`, the
  platform's loop, one identity per deployment, every tool running without
  approval in this iteration; "Details likely to change"
  -- the configuration sketch gains a server table.
- [specs/runs.md](../specs/runs.md): "Tools" -- from planned to behaviour;
  "A tool declares whether it is safe to execute again" becomes the MCP
  annotations and what the platform does with them after an interruption;
  "In the layout" -- the order of a run's events gains the tool-call and
  tool messages.
- [specs/conversations.md](../specs/conversations.md): the content table's
  tool row; "A turn is a chain" gains that the results of one call batch
  are one tool message (decision 7); "Details
  likely to change" loses "Fitting a long history into a model's context is
  done above the agent port" and the character-counting placeholder;
  "Reasoning" and "What crosses a swap" say what `extras` holds: the
  vendor's signed thinking blocks, keyed by vendor, replayed to that vendor
  alone, and never any reasoning text (decision 8).
- [ADR 0002](../adr/0002-conversation-persistence.md): superseded in part by
  ADR 0004 (the "anything that must behave identically under both
  frameworks lives above the agent port" paragraph and its trimming
  example). The rest stands.
- [layout.md](../layout.md): core's list loses "history trimming" and "trimming
  or summarising-selection of a history to fit a context window"; ports gain
  `ToolServers`; the responsibilities table moves "fitting a history into a
  context window" to the agent adapters and adds the MCP adapter and its
  import rule; the dependency matrix does not change.
- [specs/wire.md](../specs/wire.md): "Tool calls, when they come, already
  have AG-UI events" becomes the mapping; the client's "does not do" list
  loses tool-call events.
- [specs/core.md](../specs/core.md), "Tool usage, over MCP. The first version
  has none": no longer the case.

## Worth not relearning

Kept from the notes this plan replaces, condensed.

- **The vendor clients are pinned** -- endpoint from the configuration, key
  as a header, proxy left to `HTTPS_PROXY`, `max_retries=0`, tracing forced
  off, request-body loggers held at `WARNING`. The MCP adapter follows every
  one of those rules; check what the SDK logs at `DEBUG` before it is
  adopted, since `ANTHROPIC_LOG=debug` wrote whole conversations to standard
  error before the vendor loggers were pinned.
- **Every `schema.sql` edit re-pins the hash and leaves `SCHEMA_VERSION` at
  1**; there are no migrations before the first release.
- **Tool arguments and results are attacker-influenced text** going to a
  model and to a browser: bounded on the way in, stored as data, rendered as
  data.
- **Reviews find the most in three places**: refusals that reflect input,
  no-ops on re-attach, and a hand-written parser doing what a real one
  already does.
- `demo/start.sh` is the fastest way to see a change end to end with a real
  model through OpenRouter; `scripts/rehearse-deployment.sh` covers sign-in
  without real providers.
