# Robinauts — project layout and architecture

Status: draft v1. Goals and design principles are in
[specs/core.md](specs/core.md); decisions are in [adr/](adr/).

The backend follows a variation of hexagonal architecture. The package is
split into layers with strict, one-directional dependencies. The rules are
enforced mechanically from the first scaffold (see section 5).

## 1. Directory tree

```
robinauts/
  .github/                    # the CI workflow and Dependabot
  docs/
  scripts/                    # one script per check; CI runs these
  demo/                       # one command to run the whole thing on one machine
  frontend/                   # the UI (ADR 0001)
  backend/
    pyproject.toml
    hatch_build.py            # build hooks: the frontend and the licence files
    main.py                   # infrastructure: script wrapper
    src/robinauts/
      __init__.py
      app.py                  # infrastructure: composition root, exposes create_app()
      cli.py                  # infrastructure: argument parsing, the `robinauts` console script
      domain/                 # dataclasses, enums, constants, exceptions
      core/                   # pure functions, no IO
      ports/                  # ABCs the application depends on
      application/            # control flow and business rules
      api/                    # inbound HTTP: translates requests into application calls
      adapters/               # communication with the external world
        agents/
          langgraph/          # the ONLY place LangGraph / LangChain are imported
          pydantic_ai/        # the ONLY place Pydantic AI is imported
        tools/
          mcp/                # the ONLY place the MCP SDK is imported
      datastore/              # adapters for owned state (the database)
    tests/
```

The package lives under `src/` so that the root-level `main.py` never
shadows it. `main.py` is a minimal `if __name__ == "__main__"` wrapper
around `robinauts.cli.run()`.

## 2. Layers

### domain

Shared vocabulary. Dataclasses, enums, constants and exceptions: the
platform's own conversation format (`Conversation`, `Message`, message
parts, `Role`), `TurnEvent` (what a running turn streams) and `EngineEvent`
(what an agent engine yields, which carries none of the platform's ids),
`UsageRecord`,
`Principal`, `RobinautsError` and friends.

The conversation format is the one ADR 0002 refers to: owned by the
platform, not shaped by any agent framework or model vendor.

Minimal logic is accepted with caution: the validation of its own values in
`__post_init__`, simple derived properties, `reading_stored` (how a store
turns its own columns into a flat record, which is here because a store may
import domain and must not import core), and the functions that make a model
provider's text into content this format can hold — `clean_text`, which
repairs it, and `text_parts`, which splits what is longer than one part. The
rule about what the format may hold and the operations that satisfy it are one
subject, and their callers are the agent adapters, which may import domain and
must not import core. For the same reason — its callers are the agent adapters
— one rule over a path is here too: `unanswered_calls`, which calls of a
visible path no tool message answers, with `NO_RESULT`, what a model is told
of one (`domain.tools`). Anything more belongs in core.

Depends on nothing inside robinauts. Everything may depend on it.

### core

Pure functions. No IO, no clock, no randomness, no global state. Every
function is testable with input and output alone.

Complicated logic must live here: validating a conversation, aggregating
usage records, matching an identity against the sign-in allow list,
checking ID token claims (with `now` passed in), config validation from raw
dicts into domain objects, naming and bounding the tools a run is handed.
**Not** fitting a history into a context window: that is each agent
adapter's own policy ([ADR 0004](adr/0004-context-management-in-the-adapter.md)).

What it owns for conversations and runs: the **one canonical encoding** of
the format and its versions (`message_to_data` / `message_from_data`,
`run_event_to_data` / `run_event_from_data`, and the upgrades — two document
shapes and no more), the rules of the **tree** (`ConversationTree`, built by
`tree_of` or `tree_of_stored` and asked everything afterwards: what may follow
what, paths, which path is the visible thread, where an edit or a
regeneration attaches), the **run state machine** and "one active run", the
**two order checks** (what an engine yields, what the application publishes,
and where a watcher re-attaches), the derived **title**, and the **naming of
a run's tools** (`<prefix>__<name>`, sorted, a name that does not fit left
out). Reading our own rows goes through the `*_stored` readers, so a fault in
stored data is never answered as a fault of the request. Their callers are
the **application**: `datastore` may not import core, so a store is handed
the document core wrote and hands it back unread.

Depends on domain only.

### ports

Abstract base classes describing what the application needs from the
outside world. To be refined as the specs grow.

There so far:

- `CredentialStore`: sessions, pending sign-ins, API tokens.
- `IdentityProvider`: the OIDC exchange with a sign-in provider — one GET
  and one POST, returning raw data and deciding nothing.
- `Clock`: current time, as an aware datetime, and a monotonic count for
  measuring intervals.
- `SecretSource`: fresh unguessable text — the `state`, the `nonce`, the
  PKCE verifier and the session cookie's value. Everything the platform draws
  that must be **unguessable** comes from here.
- `ConversationStore`: conversations, the messages of their trees, the runs
  over them and a run's numbered events — **one port**, because they are one
  database and several operations over them are one transaction (beginning a
  turn, completing a message, ending a run, deleting a conversation). A
  conversation and a run cross as records; a message and a run event cross as
  the **document** core wrote, with the record beside it for the store's own
  columns. It is where "at most one active run per conversation" is held, and
  where an event at a position that is not the next one is refused.
- `RunExecutor`: execute the work of a run in the background and cancel
  it. It is handed a run's id and something to run, and it decides nothing
  about runs: which run may be executed, what a cancellation means and what
  is written when work stops are the application's. Version 1 is asyncio
  tasks in the backend process; a later `robinauts worker` claiming runs
  from the database would be another implementation, and the application
  would not know.
- `RunSignals`: how a watcher learns that a run has stored something new,
  without asking the database over and over. It carries **no data** —
  "this run now reaches this position", and the watcher reads the store —
  every wait under it is bounded, and a signal that is lost costs a wait and
  never an event. What was said is remembered for a bounded while, the end of
  a run included, so that a watcher asking about a position it already holds
  is answered rather than made to wait. One process today; PostgreSQL
  `LISTEN`/`NOTIFY` later, with nothing above the port changing.
- `IdSource`: a fresh id for something about to be stored. The records of a
  turn name each other, so they are built before any of them is stored. It is
  the platform's other source of randomness, and deliberately not the same
  one: an id is public and goes in a URL, and a test's id source may be
  predictable where a test's secrets may not.

- `Agent`: run one turn — given the agent's definition, the full visible
  path ending in the user message being answered (never trimmed above the
  port, [ADR 0004](adr/0004-context-management-in-the-adapter.md)) and the
  tools the run has, stream the engine's own `EngineEvent`s as an **async
  generator**, which the application closes to release what the engine
  holds. A turn ends finished, or waiting on the tool calls it yielded; an
  engine never executes a tool. Failure is reported by raising and a
  cancellation is let through. The application turns those events into
  messages and `TurnEvent`s: an engine has no ids, no clock and no rows.
  Implementations: LangGraph and Pydantic AI.
- `ToolServers`: the configured MCP servers, one port over all of them keyed
  by server id, because the application never holds a client — list the
  tools of a server, call one with arguments under a timeout, answer with
  the platform's own result. Its docstring says what the vendor clients'
  do: no environment fallbacks, nothing phones home, no arguments or
  results in a log. Implementation: the MCP adapter, a client of our own
  over `httpx` (below).

Still to come:

- `UsageStore`: record and query token usage per model and conversation.

**Configuration has no port**, deliberately. An adapter function reads the
file and returns the raw tables (`adapters.read_toml`); `core` turns them
into domain objects (`core.parse_sign_in_config`); the composition root
calls the two in turn and hands the result to whatever needs it. A port is
a seam for something the application does at run time and a test must
replace — and nothing above `adapters` reads configuration at run time: it
is given the domain objects, already validated, at start-up. An earlier
draft of this document listed a `ConfigSource` port; there is none, and
there is no place for one.

Depends on domain only.

### application

Control flow and business rules. Orchestrates a turn as a run
([specs/runs.md](specs/runs.md), ADR 0002): create the run, load the
history, call the agent port, publish events, append each new message as it
is produced, then finish, suspend or fail the run. Also the sign-in flow, conversation management
(list, rename, delete) and usage export.

This is the "controller" of the core spec: it knows the `Agent` port and
nothing about any agent framework.

Receives all port implementations by injection; never constructs them.

Stateful through the store ports, and **in the process only where the state
is one of three things**: a cache of something public the deployment fetched
(a provider's OpenID discovery document, with the one fetch of it that is in
flight, which is what keeps ten callers to one fetch); a scheduling hint
(when expired rows were last swept); or a diagnostic counter, which exists to
be read by an operator and by no code at all (how many sweeps failed, and
what the last one raised, until there is somewhere to log it). Nothing else.
In particular, nothing a request's correctness or its security may depend on:
no session, no pending sign-in, no ownership, no counter that enforces a
limit. The test is whether losing it is invisible — a cache is rebuilt by
asking again, a hint means one extra sweep, a counter means a number nobody
read — and whether a second process, which has its own copy, would disagree
about anything that matters. Each process therefore sweeps on its own
schedule and discovers for itself; both are safe, because the answers are the
same for everyone and the rows they touch are already expired.

Depends on ports, core and domain.

### api

The inbound side: HTTP routes and the streaming endpoint the UI talks to.
It translates requests into application calls and application results into
responses. It decides nothing.

A server needs an inbound side. It is kept apart from `adapters` because it
points the other way: it calls the application, where an adapter is called
by it.

Depends on application and domain. Must not reference core, ports,
adapters or datastore.

### adapters

Communication with the external world: the two agent engines, the OIDC
client, the config reader.

Each agent adapter translates between the platform's conversation format
and its framework's format, in both directions, on every turn (ADR 0002),
and owns its **context policy** — what of the full path it sends, in what
order, with which cache breakpoints (ADR 0004). The MCP adapter
(`adapters/tools/mcp/`) is the one `ToolServers` implementation, pinned as
the vendor clients are: endpoint from the configuration, credential as a
header, no retries the application cannot see, `HTTPS_PROXY` obeyed and
nothing vendor-specific. It is a **client of our own over `httpx`**: the MCP
Python SDK's dependency tree fails the licence gate
([DEPENDENCIES.md](../DEPENDENCIES.md), "Known exclusions"), and the plan's
fallback for that case is the three JSON-RPC calls a client needs, written
here. The frameworks and the SDK are confined to their own sub-package:

- only `adapters/agents/langgraph/` may import `langgraph`, `langchain`,
  `langchain_core`, a `langchain_*` provider client, or `langsmith` — which
  that adapter imports for exactly one call, the one that turns hosted
  tracing off, and which is named in the rule so that "nothing phones home"
  cannot become an import somewhere nobody was looking;
- only `adapters/agents/pydantic_ai/` may import `pydantic_ai`,
  `pydantic_graph`, `logfire`, `logfire_api` or `opentelemetry` — the last
  three arrive with the framework, are imported by nothing in the platform,
  and are named in the rule for the same reason `langsmith` is;
- only `adapters/tools/mcp/` may import `mcp`, the MCP Python SDK — a rule
  written before the import exists, since the SDK is not adopted today, so
  that the day its tree passes the gate it belongs there and nowhere else;
- the two agent adapters do not import each other.

**How that is enforced**, which matters as much as the rule: the contract's
source is the whole `robinauts.adapters` **package**, so a module added to
that layer tomorrow is inside the rule without anybody remembering to list
it, and the one sub-package that may import the framework is written as an
*exception* to the rule rather than as an omission from it. A test writes a
module into `adapters/` that imports the framework and asserts the contract
breaks (`tests/unit/test_architecture.py`).

This is the backend's counterpart of the frontend seam in ADR 0001.
**The discard test:** five places name the adapter, and deleting the
sub-package and its dependencies must break those and nothing else: the
import in `app.py` and its one entry in that module's `ENGINES` table; the
contract exceptions in `backend/pyproject.toml` that name the sub-package;
the sub-package's own tests; and the **shared swap fixtures** under
`backend/tests/` — `engines.py`, `unit/test_engine_swap.py` and the
configuration swap in `integration/test_create_app.py` — which exist to name
both engines at once and cannot be written without both.

Plus one that names no adapter and would fail all the same: the composition
tests (`unit/test_app_composition.py`) assert that *both* engines are wired,
which is a claim about the table and not about either sub-package. Removing
an engine is meant to be noticed there.

Depends on ports and domain. Must not reference core, datastore,
application or api.

### datastore

A special case of adapter for owned state: where application state lives.
It implements the store ports (`ConversationStore` — which owns
conversations, messages, runs and run events — `UsageStore`,
`CredentialStore`) over the one database of the deployment. Its schema is
entirely the platform's; no framework creates or migrates tables in it
(ADR 0002).

It **never parses or builds a message or a run event**. Those are written in
the platform's own format, the format is core's, and a store may not import
core. What crosses the port for them is the **document** — the whole message, or
one run event with its position, as the plain JSON-able mapping core writes —
which a store keeps as jsonb and hands back untouched; it is given the record
beside it when it needs a field for one of its own indexed columns (id,
conversation id, parent id, created at, seq).
The flat records whose columns it does own — `Conversation`, `Run`, `User` —
it builds itself, inside `domain.reading_stored`.

Same dependency rules as adapters: ports and domain only.

### infrastructure

`src/robinauts/app.py`, `src/robinauts/cli.py` and `main.py`. Builds the
concrete adapters and datastore, injects them into the application, mounts
the api, and exposes `create_app()`. This is the only place that references
adapters, datastore, application and api together — and therefore the only
place where the choice of agent engine is made. `cli.py` is also installed
as the `robinauts` console script, and is where uvicorn is run and logging
is configured -- a library configures neither, and a command is the one
program that may.

## 3. Dependency matrix

Rows may import columns marked ✓.

| from \ to        | domain | core | ports | application | api | adapters | datastore |
|------------------|:------:|:----:|:-----:|:-----------:|:---:|:--------:|:---------:|
| domain           |   –    |      |       |             |     |          |           |
| core             |   ✓    |  –   |       |             |     |          |           |
| ports            |   ✓    |      |   –   |             |     |          |           |
| application      |   ✓    |  ✓   |   ✓   |      –      |     |          |           |
| api              |   ✓    |      |       |      ✓      |  –  |          |           |
| adapters         |   ✓    |      |   ✓   |             |     |    –     |           |
| datastore        |   ✓    |      |   ✓   |             |     |          |     –     |
| infrastructure   |   ✓    |  ✓   |   ✓   |      ✓      |  ✓  |    ✓     |     ✓     |

Third-party libraries: domain and core use the standard library plus pure
parsing or validation libraries where needed, as long as the function stays
free of IO. The web framework (FastAPI) is confined to api and
infrastructure; `ag-ui-protocol` is confined to **api alone**, since nothing
outside it has an AG-UI event to build — the composition root included. The
database driver (`asyncpg`) is confined to datastore. Agent frameworks are
confined to their adapter sub-package, and so is the MCP SDK. HTTP clients
are confined to adapters.

## 4. Responsibilities by concern

| concern | layer |
|---|---|
| the platform's conversation format: the records, the two event vocabularies, the value rules (`clean_text`, `text_parts`) | domain |
| the canonical encoding of the format and its versions | core |
| the rules of the tree: paths, the visible thread, where a message attaches | core |
| the run state machine, and the order of a run's and an engine's events | core |
| the derived title | core |
| reading our own rows: flat records from columns (`domain.reading_stored`) | datastore |
| reading our own rows: messages and events from their documents | core's `*_stored` readers, called by application |
| fitting a history into a context window, prompt caching | adapters (each agent adapter, its own policy; ADR 0004) |
| the tool loop: fetching the run's tools, calling them, appending the result, running the engine again | application |
| naming, sorting and bounding a run's tools | core, called by application |
| talking to tool servers, the credential in use | adapters (the MCP adapter) |
| the turn and run lifecycle (ADR 0002, specs/runs.md) | application |
| executing runs in the background | adapters (run executor) |
| saying that a run has stored something new | adapters (run signals) |
| delivering a run's events to whoever may see them | application (the watcher), over the store and the signals |
| run records and events at rest | datastore |
| platform format <-> framework format | adapters (each agent adapter) |
| talking to model providers, API keys in use | adapters (agent adapters) |
| conversations, messages, usage, sessions at rest | datastore |
| token usage per model and conversation | application records, datastore stores, core aggregates |
| allow-list matching, ID token claim checks | core, with `now` from the clock port |
| OIDC discovery and code exchange | adapters (identity provider) |
| cookies, CSRF checks, request parsing, streaming responses | api |
| config reading, validation into domain | adapters read (raw data), core validates, infrastructure wires — no port |
| wiring, injection, choice of agent engine | infrastructure |

## 5. Enforcement

The dependency matrix is enforced with
[import-linter](https://import-linter.readthedocs.io/) contracts declared
in `backend/pyproject.toml` and run in the test suite
(`tests/unit/test_architecture.py`) from the first scaffold. Contracts
cover:

- domain imports nothing from robinauts
- core imports only domain
- ports import only domain
- application imports only ports, core, domain
- api imports only application and domain
- adapters and datastore import only ports and domain
- nothing except infrastructure imports adapters, datastore or api
- `asyncpg` is imported only under `datastore`
- `httpx` is imported only under `adapters`
- FastAPI, Starlette and uvicorn are imported only under `api` and in the
  composition root (`app.py`, `cli.py`)
- `ag_ui` is imported only under `api`
- LangGraph, LangChain, its provider clients and `langsmith` are imported
  only under `adapters/agents/langgraph`
- Pydantic AI, `pydantic_graph`, logfire and OpenTelemetry are imported only
  under `adapters/agents/pydantic_ai`
- the MCP SDK (`mcp`) is imported only under `adapters/tools/mcp`
- the two agent adapters do not import each other

## 6. Testing strategy

- **core and domain**: plain unit tests, input and output only.
- **application**: unit tests with in-memory fakes for every port (fake
  agent, fake clock, fake stores, fake identity provider). This is where
  the turn lifecycle, sign-in rules and usage recording are proven.
- **agent adapters**: one shared contract suite that both implementations
  must pass, run against recorded or stubbed model responses. It includes
  the swap: a conversation started on one adapter continues on the other.
  Live-provider tests, if any, are opt-in.
- **datastore**: the store contract suites against a real database.
- **api**: route tests over the application wired with fakes.
- **infrastructure**: one smoke test that wires everything and runs a turn
  end to end against a stubbed model.

Tests that touch the network, the file system, a database or a local
server carry the `io` marker.

## 7. Conventions

- Python 3.12, `pyproject.toml`, src layout, pytest, ruff, black, line
  length 100.
- Ports are ABCs; fakes for tests live under `tests/` and implement the
  same ABCs.
  <!-- REUSE-IgnoreStart -->
- Every source file starts with the licence header:
  `# SPDX-License-Identifier: Apache-2.0` and
  `# Copyright The Robinauts Authors`.
  <!-- REUSE-IgnoreEnd -->
