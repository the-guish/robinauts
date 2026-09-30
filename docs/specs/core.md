# Robinauts — Core Spec

Robinauts is a conversational agents platform: a web UI, a backend and a set
of libraries that a company runs on its own servers.

The specs describe the whole platform. Which parts make the first version
is decided later, as a cut through them; "planned" means named but not yet
specified.

This document is the stable view: the goals, the principles, and the shape
of the system. The details, and the things likely to change, are in the
topic documents listed under [Documents](#documents).

## Goals

### 1. Open source without restrictions

- Everything is Apache-2.0: the platform, the tools and the libraries.
- It is safe to run as an internal corporate platform and safe to embed in a
  commercial product. No component adds a restriction to either use.
- Every dependency is scanned, transitively, against a licence allowlist. A
  dependency that fails the scan breaks the build.

### 2. Secure and corporate-ready

- Security is a primary design constraint, not a later hardening pass.
- Sign-in is through the company's identity provider (Okta, Google).
- The platform fits how a company operates software: access control, supply
  chain hygiene, and a deployment an internal platform team can own.

### 3. Data stays on the hosting company's servers

- Conversations, history, memories and all other stored data live only in the
  hosted environment.
- The platform sends nothing to the Robinauts project or to any third party:
  no telemetry, no CDN, no hosted service it depends on.
- The only data that leaves is what a conversation sends to the model
  providers the operator has configured (goal 5), and what a model asks a
  configured tool server ([agents.md](agents.md), "Tools").

### 4. Compose over build

- Select the best existing component for each part. Build only the glue and
  the parts that are missing.
- Components talk to each other through open, vendor-neutral standards:
  MCP, A2A and the emerging AG-UI.
- As a result the platform is extensible and its components are swappable.

### 5. Bring your own API key

- The operator supplies their own model provider credentials. The
  configuration names an environment variable per provider; keys are never
  stored in the database and never reach the browser. Users do not supply
  keys.
- The platform works well with any major vendor: OpenAI, Anthropic, Gemini,
  OpenRouter, AWS Bedrock.
- No vendor is privileged; switching or mixing providers is ordinary use.

### 6. Minimal deployment footprint

- One frontend (the UI), one backend, one database. Nothing else is required
  to run the platform.

### 7. Usage reporting (planned)

- The platform will track token usage per model and per conversation, and
  provide an API to export the records, so a company can feed them into its
  own reporting or cost tooling.
- All of it — recording, API, screens — is planned for later and is
  specified then. What is already settled is in [operations.md](operations.md).

## Design principles

### Hexagonal architecture

- The backend follows hexagonal architecture (ports and adapters).
- The core holds the application logic and depends on nothing outside
  itself. Everything external — the database, the model providers, the
  agent framework, the HTTP layer — is reached through a port and
  implemented by an adapter.
- A port is a Python abstract base class (`abc.ABC`).
- The layers and their dependency rules are in [layout.md](../layout.md),
  and are enforced mechanically.

### Components are swappable, and the seams are explicit

- A component chosen today can be discarded tomorrow. Each one is confined
  behind a seam that is written down and enforced by a check, and each has
  a **discard test**: removing it must break one known place and nothing
  else.
- The seams so far:
  - the chat UI library (assistant-ui) —
    [ADR 0001](../adr/0001-chat-ui-assistant-ui-with-tailwind.md),
    [frontend.md](frontend.md);
  - the agent frameworks (LangGraph, Pydantic AI) —
    [agents.md](agents.md), [layout.md](../layout.md);
  - the wire between UI and backend, which is a published standard —
    [wire.md](wire.md).

### The agent engine is a port

- The controller — the main application flow — is independent of any agent
  framework. It knows only the agent port: hand the adapter a question and
  the conversation's memory, and write down what it streams. The tool
  loop, the context and the memory are the framework's, behind the port
  ([ADR 0005](../adr/0005-the-framework-owns-the-loop-and-the-memory.md)).
- There are two implementations: one on LangChain (or LangGraph; open
  source parts only, none of their commercial or hosted offerings), one on
  Pydantic AI.
- Both are wired in every deployment and held to one contract suite, so the
  seam is agent-framework agnostic and a third framework is a third
  adapter. A conversation stays with the engine it started on.

### The transcript is the platform's; the memory is the framework's

- Conversations are persisted in the database, in a format the platform
  owns — not the format of any agent framework or any model vendor — as
  the **transcript**: what the people reading the conversation see, what
  the exports carry, what analytics reads.
- The model's **memory** — the framework's own history of the conversation,
  in the framework's own format, with whatever it kept in it — is stored by
  the platform, on the run that produced it, as opaque data the platform
  never reads, and handed back to the same framework at the next turn.
- A framework's own persistence is never used: no checkpointer, no tables
  of a framework's in the deployment's schema.
  [ADR 0002](../adr/0002-conversation-persistence.md),
  [ADR 0005](../adr/0005-the-framework-owns-the-loop-and-the-memory.md).

### Dependency scanning from day zero

- Security scans and licence scans of dependencies exist from the first
  commit, for both the Python and the JavaScript side, and both block the
  build. [open-source.md](open-source.md).

### Nothing phones home

- No telemetry, no CDN, no hosted service, no framework's hosted tracing.
  The only outbound traffic is to the identity providers at sign-in, to the
  model providers the operator configured, and to the tool servers the
  operator configured.

## The system in one page

- **Shape.** One Python backend that also serves the built frontend, one
  PostgreSQL database. Delivered as one Python wheel.
  [backend.md](backend.md), [frontend.md](frontend.md),
  [operations.md](operations.md).
- **Sign-in.** Google and Okta over OpenID Connect, the way neorc does it;
  an allow list decides who gets in. Two roles, user and admin.
  [sign-in.md](sign-in.md).
- **Interface.** neorc's layout: a collapsible left navigation panel that
  this project owns, and the chat in the middle; the application opens on
  an empty chat. [frontend.md](frontend.md).
- **Conversations.** A tree of messages in the platform's own format —
  text, images, files, reasoning, tool calls and their results — the
  transcript of what was said; with attachments, search and export.
  [conversations.md](conversations.md).
- **Privacy.** Private by default; projects; share links; admins see
  metadata and never content; soft delete, retention, audit.
  [privacy.md](privacy.md).
- **Agents.** Named agents defined by the operator — a prompt, a model, an
  engine, and the tool servers it may use. Users pick one per conversation.
  [agents.md](agents.md).
- **Tools.** Remote MCP servers the operator configures like model
  providers; the framework's own MCP client calls them inside its loop, and
  the platform writes each call and result into the transcript as it
  happens. [agents.md](agents.md), [runs.md](runs.md).
- **A turn.** The UI posts a message, which starts a **run**: the
  controller finds the conversation's memory, hands the agent port the
  question and that memory, publishes what the adapter streams as AG-UI
  events, appends each new message as it completes, and stores the memory
  the adapter hands back. The run executes in the background and is
  persisted: if the request drops, the agent keeps working, and the UI
  re-attaches. One active run per conversation. [runs.md](runs.md),
  [agents.md](agents.md), [wire.md](wire.md).
- **Channels.** One API for every delivery channel. The web UI is the
  first client; a mobile application and a Slack bridge are planned, and
  consume the same agents and conversations through the same API.
  [channels.md](channels.md).
- **Open source.** Apache-2.0 throughout, DCO, provenance records,
  dependency gates. [open-source.md](open-source.md).

## Planned

Named, not yet specified. Where a decision today would make one of them
hard, the decision is taken with them in mind.

- Tools that suspend a run — a person's approval before a tool runs, an
  external job whose result arrives later — and per-user credentials to a
  tool server ([runs.md](runs.md), [agents.md](agents.md)).
- Memories a person keeps across conversations. When they come they live
  in the one database and are framework-neutral, like the transcript; the
  model's memory of one conversation is the framework's and is already
  stored ([agents.md](agents.md)).
- Usage reporting (goal 7).
- API tokens.
- More delivery channels: a mobile application, a Slack bridge; and a way
  to consume a run without streaming, for the clients that need it.
- Agents created by users.
- A container image.

## Open

- **The cut for the first version.**
- The smaller open points are listed at the end of each topic document.

## Documents

| document | what it holds |
|---|---|
| [core.md](core.md) | goals, principles, the system in one page |
| [sign-in.md](sign-in.md) | sign-in, users, roles |
| [conversations.md](conversations.md) | the message tree, features, deletion |
| [privacy.md](privacy.md) | visibility, projects, sharing, admins, retention, leavers, audit |
| [agents.md](agents.md) | agents, the agent port, the turn, engines, model providers |
| [runs.md](runs.md) | runs: background execution, persistence, re-attaching, tools of any duration |
| [wire.md](wire.md) | the UI-to-backend protocol |
| [channels.md](channels.md) | one API for many delivery channels: web, mobile, Slack |
| [backend.md](backend.md) | web framework, background work, database, schema |
| [frontend.md](frontend.md) | the interface, build, supply chain, packaging |
| [operations.md](operations.md) | deployment, configuration, limits, usage (planned) |
| [open-source.md](open-source.md) | licence, contributions, dependency policy, checks |
| [../layout.md](../layout.md) | backend layers and their enforced dependency rules |
| [../oss-checklist.md](../oss-checklist.md) | the open source to-do list |
| [../adr/](../adr/README.md) | decisions that needed a discussion, and why they went the way they did |
