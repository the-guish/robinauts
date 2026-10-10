# Connectors: background

Working notes, warm-up. What was looked at before deciding how chat platforms
(Slack, Telegram, Teams and the rest) reach a Robinauts deployment, what was
tried, what was learnt, and what is still open. The decision itself is short
and lives in [plan.md](plan.md); this file is the reasoning behind it.

Written 2026-10-06 to 2026-10-10. Versions and dates below are those of that
week, and will age.

## Contents

1. [The question](#1-the-question)
2. [What Robinauts already has](#2-what-robinauts-already-has)
3. [The ecosystem](#3-the-ecosystem)
4. [The TypeScript proof of concept](#4-the-typescript-proof-of-concept)
5. [Why Python](#5-why-python)
6. [Two ways of receiving: connect and serve](#6-two-ways-of-receiving-connect-and-serve)
7. [The platforms, by way of receiving](#7-the-platforms-by-way-of-receiving)
8. [Naming the two ways](#8-naming-the-two-ways)
9. [Packaging and imports](#9-packaging-and-imports)
10. [The API a connector talks to](#10-the-api-a-connector-talks-to)
11. [Security and privacy](#11-security-and-privacy)
12. [Open questions](#12-open-questions)
13. [Sources](#13-sources)

## 1. The question

Can an extra service, with little code of our own, reuse an ecosystem's
existing chat-platform integrations and drive a Robinauts agent through our
API, while the core stays as it is? In pseudo-code:

    for message in platform.new_messages():
        answer = robinauts.turn(message)
        platform.stream(answer)

The two ecosystems named at the start were **CopilotKit** and **OpenClaw**.
The API could change as needed.

## 2. What Robinauts already has

- **One API for every delivery channel** (`docs/specs/core.md`, "Channels").
  The web UI is the first client; a mobile application and a Slack bridge
  were already planned to use the same agents and conversations through the
  same API.
- **The wire is AG-UI over server-sent events** (`docs/specs/wire.md`), but a
  *profile* of it: the server loads the history from its own store and never
  takes one from the client, so the request is not AG-UI's stock
  `RunAgentInput`. Turns run in the background and a client re-attaches by
  position; a turn's stream is a view of it, not the turn.
- **The history is the server's.** Since ADR 0005 the framework keeps the
  model's memory and the platform keeps the transcript. A connector never has
  to send, nor may it send, a conversation's history: it sends the newest
  message and nothing else.
- **People own their conversations** (`docs/specs/privacy.md`): a conversation
  is private to whoever started it.
- **Credentials**: sign-in through an identity provider, sessions, and API
  tokens a person mints for themselves (`docs/specs/sign-in.md`). "Tokens
  minted by an operator for somebody else, and scopes" are listed as not
  there yet. That is exactly what a connector needs, since it speaks for many
  people.
- The interface's message view already carries `channel: Literal["web"]`
  (`backend/src/robinauts/web/app.py`), a field waiting for other values.

## 3. The ecosystem

### 3.1 CopilotKit Channels SDK

Open-sourced on 2026-08-04, MIT. "Bring any AG-UI agent to any channel."
TypeScript only. Packages: `@copilotkit/channels` (everything), or
`@copilotkit/channels-core` plus one package per platform:
`channels-slack`, `channels-teams`, `channels-discord`, `channels-telegram`,
`channels-whatsapp`. Version 0.11.0, read in full for this note.

How it is meant to run:

- **Managed**, through *CopilotKit Intelligence*, their hosted service. It
  holds the Slack and Teams credentials, receives the platforms' traffic, and
  reaches the customer's runtime over a websocket the runtime opens. This is
  a hosted relay in the path of every message: goal 3 of `core.md` (data
  stays on the company's servers, nothing phones home) rules it out.
- **Direct adapters**, which run in the customer's process and hold the
  platform credentials themselves: Slack (Socket Mode by default, or HTTP with
  a signing secret), Teams (Microsoft 365 Agents SDK, `POST /api/messages`),
  Discord, Telegram (grammY, long polling by default) and WhatsApp (webhook).

What reading the source showed:

- **A Channel has no public `start()`.** Its lifecycle belongs to a "runner".
  The runner CopilotKit ships, `CopilotRuntime`'s channel manager, always
  tries the managed activation first and falls back to the direct adapters
  only when the managed setup reports `setup_required`. The seam that runner
  drives is `channel.ɵruntime.start()` / `stop()`, internal by name. Running
  without Intelligence means calling it, with the SDK version pinned.
- **The AG-UI client is pinned before 1.0.** `channels-core` 0.11.0 depends on
  `@ag-ui/client` 0.0.59, while `@copilotkit/runtime` 1.77 is already on
  1.0.1. The same lag had already kept `@assistant-ui/react-ag-ui` out of the
  frontend (`docs/specs/wire.md`).
- **0.0.59 refuses a cancelled outcome.** Its `RUN_FINISHED` schema allows only
  `success` and `interrupt`. Our wire ends a cancelled turn with AG-UI 1.0's
  `cancelled` outcome, so a stock client needs a cancelled turn as
  `RUN_ERROR`.
- **Its event verification accepts our stream as is**: a text message and
  tool calls open at once, `TOOL_CALL_RESULT`, `REASONING_MESSAGE_*`, and the
  `metadata` field. Checked against a real backend on the echo engine.
- **Only the channel's own tools are executed.** After a run, the SDK runs
  the tool calls it captured whose names it registered, and ignores the
  rest. Our tool calls happen on the server and stream with their results,
  so the SDK never answers them a second time.
- **Thread ids differ by platform.** Telegram's conversation store gives each
  conversation a stable `tg-thread-<key>` and keeps a capped in-memory history
  (the Bot API cannot read a chat's history). Slack rebuilds the history from
  Slack's API and mints a **fresh thread id per turn**, so the AG-UI thread id
  cannot name a Robinauts conversation. The stable name is the thread's
  `conversationKey` (Telegram: `tg:<chat>:dm`, `tg:<chat>:topic:<id>`, or
  `tg:<chat>:user:<id>` in an ordinary group).
- **Identity "platform" is `provider:tenant:actor`, and Telegram's tenant is
  the chat.** The same Telegram person would be a different user in every
  chat. A custom `identifyUser` fixes it (`telegram:<user id>`;
  `slack:<team>:<member>`).
- **Telemetry is on by default**: anonymous, metadata only, 5 % sampled, sent
  to `telemetry.copilotkit.ai` (Segment underneath), switched off by
  `COPILOTKIT_TELEMETRY_DISABLED=true` or `DO_NOT_TRACK=1`. An install id is
  written under `node_modules/.cache/copilotkit/`.
- **Size**: `@copilotkit/channels` with `@copilotkit/runtime` is 343 MB of
  `node_modules`; `channels-core` with the Slack and Telegram packages alone
  is 88 MB in production. Every licence in the tree is permissive (MIT, ISC,
  Apache-2.0, BSD, 0BSD).

The fit is good on paper: it drives a stock AG-UI agent, and we serve AG-UI.
In practice it needs a stock-AG-UI endpoint on our side, an internal start
hook, a reshaping of every request on its way out (section 4), and telemetry
switched off.

### 3.2 OpenClaw

MIT, TypeScript, a *personal* assistant gateway with many channels: WhatsApp,
Telegram, Slack, Discord, Signal, iMessage, Teams, Matrix and more. It offers
three ways in for an outside agent:

- **As a model provider** (`api: "openai-responses"`, our endpoint serving
  `POST /v1/responses`). OpenClaw's own agent loop then runs on top: its
  system prompt, its tools, its history replayed every turn. Robinauts would
  be a "model" inside another agent: two loops, two memories, tools the
  outer one defines and the inner one cannot run. That is against ADR 0005.
- **As a CLI backend**: OpenClaw runs a command, sends the prompt on stdin or
  as an argument, reads text, JSON or JSONL back, and keeps a session id per
  conversation that it passes on resume. Its own loop is bypassed. A small
  command calling our API would be enough.
- **As an ACP harness** (Agent Client Protocol). Richer (tool-call updates,
  cancellation), but aimed at coding harnesses, and its plugin APIs are
  marked experimental.

The deciding fact is the security model: one trusted operator per gateway.
OpenClaw is "not a hostile multi-tenant security boundary", and no sender
identity reaches a CLI backend. It suits a personal or team bot. It does not
suit a company-wide assistant, where every person must be their own user.

### 3.3 Other candidates

| | Language | Licence | State, 2026-10 | Fit |
|---|---|---|---|---|
| Vercel Chat SDK (`chat`, `@chat-adapter/*`) | TS | open source | active; Slack, Teams, Google Chat, Discord, GitHub, Linear, Telegram, WhatsApp | the same shape as CopilotKit's, not AG-UI |
| `chat-py` (desplega-ai), a port of the above | Python | MIT | v0.1, one star; Telegram and WhatsApp are stubs | too early |
| `chatnec` | Python | MIT | 0 stars, 10 commits, no streaming | too early |
| opsdroid | Python | Apache-2.0 | 0.31.0, 2025-04-30; built for ChatOps | no streamed replies; slowing down |
| LangBot | Python | Apache-2.0 | active, many platforms | a platform with its own UI, database and LLM pipeline; brings `python-telegram-bot` (LGPL-3.0) |
| `python-telegram-bot` | Python | LGPL-3.0 | mature | refused by `DEPENDENCIES.md` |
| **`aiogram`** | Python | MIT | 3.31.0, 2026-08-26; Bot API 10.1 | good |
| **`slack-bolt` / `slack-sdk`** | Python | MIT | 1.30.0, 2026-07; 3.45.0, 2026-10 | good |
| Microsoft 365 Agents SDK | Python, TS | MIT | Microsoft's successor to Bot Framework | the way to Teams; Teams is reached through Azure Bot Service whatever the SDK |

Two platform features changed the cost of doing it ourselves:

- **Slack streams natively.** `chat.startStream`, `chat.appendStream` and
  `chat.stopStream` (2025-10), wrapped by `slack_sdk`'s `ChatStream` /
  `AsyncChatStream` helper, which buffers and keeps the state. No more
  `chat.update` loops.
- **Telegram streams natively.** `sendMessageDraft`, fully released in Bot
  API 9.5 (2026-03-01), shows a partial message as it is written, in private
  chats, groups and topics; the draft is a 30-second preview and the final
  text is sent with `sendMessage`. `aiogram` has it.

## 4. The TypeScript proof of concept

Built on the branch `claude/bold-faraday-592ykc` (two commits on top of
`ac7c58b`), to find out what the SDK route really costs. It is not on `main`.

**The backend side** (`backend/src/robinauts/web/channels.py`, about 250
lines added with `app.py` and `cli.py`, and 214 lines of tests):

- `POST /api/channels/agents/{agent_id}/agui`, served only when the server
  starts with `ROBINAUTS_CHANNELS_SECRET` (32 characters at least; a shorter
  one stops the start).
- The caller sends that secret as a bearer, compared in constant time, and is
  refused before its body is read.
- The body is AG-UI's `RunAgentInput`, of which three things are read:
  `threadId` (a conversation's id continues it; anything else starts one, named
  in `X-Robinauts-Conversation-Id`); the text of the **last user message**; and
  `forwardedProps.robinauts.user`, the platform person.
- That person is a user under the provider `!channel`, which no sign-in
  configuration can name: the holder of the secret can speak for platform
  users and for nobody who signs in on the web.
- A new message goes under the conversation's last answer, or replaces a
  question nobody answered, as the interface does. A conversation of another
  user, or of another agent, is 404; a turn already going is 409.
- The stream is the web's, with the client's own `runId`, and a cancelled turn
  as `RUN_ERROR` coded `cancelled` (section 3.1).
- Checks run in an order a client can rely on: an unknown agent (404) and a
  question with no text (422) are refused before anybody is created. The
  bridge's start-up probe asks a question with no text and reads the answer.

**The bridge** (`channels/`, a container of its own): 423 lines of TypeScript
(548 with comments) and 289 of tests.

- **Platforms**: Telegram and Slack (Socket Mode).
- **Access**: Telegram requires an allow list, because anybody can find a bot;
  a stranger is told their id. Slack is limited to its workspace.
- **Thread links**: which platform thread is which conversation is kept in a
  JSON file on the container's volume.
- **Start-up checks**: a probe stops the bridge with a sentence for a wrong
  URL, secret or agent.
- **Runtime**: Node 24 runs the TypeScript as it is, with no build step.

About 90 of those 423 lines exist only to fit around the SDK: a `fetch` hook
that rewrites each AG-UI request on its way out (the SDK has no per-run
`forwardedProps`, so the thread key and the user travel as two context
entries and are moved into the body there), the internal start hook, and the
telemetry switches.

**Verified**: the backend suite; the SDK's own `HttpAgent`, with its event
verification, against a real backend on the echo engine; the real Telegram
adapter against the real backend, with the Bot API stubbed through a grammY
transformer (placeholder message, edited into the answer; a stranger told
their id); the bridge's tests over the SDK's `FakeAdapter`. **Not verified**:
a real Slack workspace, a real Telegram bot, the Docker build (no daemon, and
Docker Hub's anonymous rate limit), PostgreSQL.

**A bug found on the way**: keyed by thread alone, a Slack thread two people
write in made each person's message 404 on the other's conversation, so every
reply started a new conversation. A thread link is per person and per thread.

## 5. Why Python

Measured against the TypeScript bridge, estimated for Python (Slack Bolt and
aiogram), in code lines without comments or blanks:

| | TypeScript + SDK | Python |
|---|---|---|
| Bridge source | 423 (measured) | ~550–650 |
| Of which platform handling | ~10 | ~290 (Telegram ~150, Slack ~140) |
| Tests | 289 | ~350–450 |
| Each further platform | ~5–10 | ~120–200 |
| Backend endpoint | ~250 | the same |

What Python costs: we own the platform code the SDK would give us. That means
normalising events, choosing the conversation key, streaming within each
platform's limits (Telegram's edit rate, 4,096 characters), formatting,
ignoring Slack's retried deliveries, and one turn at a time per thread. Each
new platform is code, not a line.

What it gives:

- **One toolchain** with the backend: uv, ruff, black, pytest, import-linter,
  the Python licence gate of `DEPENDENCIES.md`, pip-audit. No second
  JavaScript project to keep apart from the frontend's bundle rules.
- **No SDK workarounds**: no internal start hook, no pre-1.0 AG-UI client, no
  telemetry to switch off, no reshaping of requests. Because we write the
  client, the request can say what the endpoint wants directly.
- **`ag-ui-protocol` is shared**: the backend encodes its events with it, and
  a connector can decode them with the same types.
- **Plain, well-maintained platform libraries** with permissive licences, each
  the reference for its platform.

The decision taken: Python.

## 6. Two ways of receiving: connect and serve

A platform delivers messages one of two ways, and the difference decides how
a connector is deployed, more than which platform it serves.

| | The connector opens the connection | The platform calls the connector |
|---|---|---|
| Examples | Slack Socket Mode, Telegram long polling, Discord Gateway | Teams, WhatsApp Cloud API, Slack Events API, Telegram webhook |
| Network | outbound only; no public URL, no inbound firewall rule | a public HTTPS URL, behind the company's reverse proxy |
| Proof of origin | the credential the connection was opened with | a signature or secret on every request, checked over the raw body |
| Deadline | none to the platform | answer fast (Slack 3 s), then work: reply through the platform's API afterwards |
| Replicas | usually one per bot token: Telegram refuses a second poller (409), Discord needs sharding at scale, Slack balances events across up to ten Socket Mode connections | any number behind a load balancer; the HTTP handler is stateless |
| Process | a long-running worker | an HTTP service (FastAPI) |

Hence two containers: one for connected platforms, single replica, nothing
inbound; one HTTP service for the others, scaled like any web service.

**Several platforms do both.** Slack has Socket Mode and the Events API.
Telegram has `getUpdates` and `setWebhook`, which exclude each other. Discord
receives messages only over its Gateway, while its HTTP interactions endpoint
carries slash commands alone. So the way of receiving is chosen per
deployment, and is a property of a connector rather than of its place in the
source tree.

A detail with consequences: **Slack does not admit Socket Mode apps to its
public Marketplace.** An internal app is fine on Socket Mode. One published
for other workspaces needs the Events API, and so the HTTP service.

## 7. The platforms, by way of receiving

**Connect** (the connector opens the connection):

| Platform | How | Notes |
|---|---|---|
| Slack | Socket Mode, `slack-bolt` | internal apps; Marketplace apps need serve |
| Telegram | long polling, `aiogram` | one poller per token |
| Discord | Gateway websocket, `discord.py` | messages only arrive this way |
| Mattermost | websocket | |
| Rocket.Chat | realtime API (websocket) | |
| Matrix | `/sync` long poll, `matrix-nio` | |
| Zulip | event-queue long poll | |
| Signal | the `signal-cli` daemon | |
| Feishu/Lark, DingTalk | long-connection ("stream") modes | both offer webhooks too |
| Google Chat | Cloud Pub/Sub pull | also offers HTTP |
| Email | IMAP IDLE | |
| XMPP, IRC | persistent connection | |

**Serve** (the platform calls the connector):

| Platform | Notes |
|---|---|
| Microsoft Teams | through Azure Bot Service, which Teams requires; Microsoft 365 Agents SDK |
| WhatsApp Cloud API | webhook only |
| Messenger, Instagram | webhook only |
| LINE, Viber | webhook only |
| Twilio (SMS, WhatsApp, voice) | webhook only |
| Webex | webhooks |
| Google Chat | HTTP mode |
| Inbound email | SendGrid, Mailgun or Postmark parse webhooks |
| GitHub, GitLab, Linear, Jira, Zendesk, Intercom, ServiceNow | the bot mentioned in a comment or ticket |
| Slack, Telegram | their webhook modes |

The first two connectors are Slack and Telegram, on connect. Teams and
WhatsApp are the likely first two on serve.

## 8. Naming the two ways

| Pair | Reads as | Weak spot |
|---|---|---|
| `socket` / `webhook` | the vendors' own words | long polling is not a socket |
| `outbound` / `inbound` | who opens the connection: the firewall question | clashes with inbound and outbound *messages* |
| **`connect` / `serve`** | what the process does | verbs, which is what a command is |
| `pull` / `push` | who starts a delivery | Socket Mode is push over a connection we opened |
| `gateway` / `webhook` | Discord's words | "gateway" is overloaded |
| `polling` / `webhook` | Telegram's words | misnames Slack and Discord |
| `workers` / `endpoints` | how each is deployed | says nothing about the network |
| `client mode` / `server mode` | the connector as the platform's client, or its server | `clients/` already means something else here |

Chosen: **`connect` / `serve`**, as the two commands and in the docs, each
described in one line: *connect: no public URL, one replica*; *serve: public
HTTPS, scales out*.

## 9. Packaging and imports

**One distribution, extras per platform.** `robinauts-connectors`, imported as
`robinauts_connectors`, with a command `robinauts-connectors`. An install takes
the platforms it serves as extras. A platform that can only be served pulls
in the `webhook` extra (FastAPI and uvicorn) itself, so nobody has to
remember it.

    pip install "robinauts-connectors[slack,telegram]"
    robinauts-connectors connect slack telegram

    pip install "robinauts-connectors[teams,whatsapp]"
    robinauts-connectors serve teams whatsapp --port 8080

On 2026-10-10 the names `robinauts`, `robinauts-connectors`,
`robinauts-client` and `robinauts-sdk` were all free on PyPI.

**Alternatives weighed**:

- *One distribution per connector* (`robinauts-connector-slack`), as LangChain
  and LlamaIndex do. More release machinery, and pointless while every
  connector lives in this repository. The entry-point group below gives the
  same openness to outside packages anyway.
- *A namespace package* (`robinauts.connectors`), sharing the server's top-level
  name, as `airflow.providers` does. Possible, since
  `backend/src/robinauts/__init__.py` holds nothing but its licence header,
  but it costs:
  - `web/cli.py` finds the interface with
    `Path(str(resources.files("robinauts")))`, which breaks once the package
    spans several directories (`importlib.resources` returns a multiplexed
    path).
  - The backend's import-linter contracts are written with `robinauts` as
    their root package.
  - Every future `robinauts-*` distribution must never ship a
    `robinauts/__init__.py`.

  A separate top-level name avoids all of it, as `langchain_openai` beside
  `langchain` does.

**No dependency on the server package, in either direction.** The server
distribution `robinauts` brings LangChain, Pydantic AI, asyncpg and the built
interface; a connector needs none of it. The contract between them is the
HTTP API: `openapi.json` for the JSON routes, AG-UI for a turn. The one
library both import is `ag-ui-protocol` (MIT, depending on pydantic alone).
Both are released from one tag with one version number until the API is
stable. The connectors check what they talk to at start-up.

**Extensible by entry points.** A connector is found through the entry-point
group `robinauts.connectors`. A company can ship its own connector as a
package of its own, without forking.

**Later, `robinauts-client`.** The connectors' API module is a small client of
the Robinauts API. It becomes a distribution of its own once a second Python
consumer exists. Android generates its own client from the same
`openapi.json`.

**Repository plumbing.** The project needs these:

- `clients/connectors/` holds a `pyproject.toml` and a `uv.lock` of its own.
  A uv workspace with the backend is the alternative; it would share a lock,
  but tie the two dependency sets together.
- The licence gate and pip-audit read `backend/uv.lock` today. They have to
  read the connectors' lock too.
- `reuse lint` needs the new files covered.
- CI needs a job.

**Import rules**, in the spirit of `docs/architecture/rules.md`: the core of
the connectors imports no platform library. Each platform library is
imported under its own sub-package and nowhere else. FastAPI is imported only
by the serve runner. `ag_ui` only by the API client. Nothing imports
`robinauts`. Written as import-linter contracts in the connectors'
`pyproject.toml`, so that they are checked rather than remembered.

## 10. The API a connector talks to

The endpoint of the proof of concept (section 4) is the starting point, to be
ported to `main`. What a connector relies on:

- **One call per turn**: a POST naming the agent, carrying the conversation
  (or "new"), the person, the question and optionally a model; the answer is
  the turn's AG-UI stream, with the conversation's id in a header.
- **The newest message only.** The history is the server's.
- **One turn at a time per conversation.** A second message to a busy
  conversation is 409. The connector serialises turns per thread, so the
  person's second message waits for the first answer instead.
- **Thread links.** Which platform thread is which conversation is kept by the
  connector today, in a file. Losing it loses the links, not the
  conversations. The store is the better home in the end: two connector
  replicas, or a lost volume, would otherwise split a thread across
  conversations.
- **Identity.** The connector names the platform person: `telegram:<id>`, or
  `slack:<team>:<member>`. The server makes that person a user of their own,
  under a provider no sign-in configuration can name. Each person in a shared
  thread has their own conversation, since a conversation is private.
- **Credentials.** A secret shared with every connector, for the proof of
  concept. The real thing is a token minted by an operator for one connector,
  naming the identities it may assert (`slack:T0123` and nothing else), with
  the allow list of `sign-in.md` applying to those identities as to any
  provider's. That is the "tokens minted by an operator for somebody else"
  `sign-in.md` lists as not there yet.
- **A stock AG-UI request, or our own?** The proof of concept took AG-UI's
  `RunAgentInput` because the SDK could only send that. A Python connector
  writes its own request, so the endpoint could take a small body of ours
  (`{"conversation", "person", "text", "model_id"}`) and keep the stock form for
  stock AG-UI clients. Either way the answer is the same AG-UI stream.
- **Android is a different kind of client.** It acts for one signed-in person,
  with that person's credentials. A connector acts for many people with a
  service credential. Both live under `clients/`; they share the API, not the
  authentication.

## 11. Security and privacy

- **The shared secret is all-powerful within its namespace.** Whoever holds it
  can speak for any platform person, but only for those. It is to be replaced
  per connector (section 10).
- **A Telegram bot is public.** Anybody can find it and spend the deployment's
  model budget through it. Hence a required allow list, with `*` said
  explicitly. Slack is bounded by its workspace.
- **Webhooks prove their origin per request**: Slack's signing secret, Telegram's
  `X-Telegram-Bot-Api-Secret-Token`, Meta's `X-Hub-Signature-256`, Teams'
  JWT from Azure Bot Service. They are verified over the raw body, before any
  parsing, and refused otherwise.
- **Tool arguments and results are attacker-influenced text** (`wire.md`). A
  connector renders them as text and never lets them become links, mentions
  or markup the platform would act on.
- **Logs** carry ids, never message text, and every platform value passes
  through the equivalent of `web/logs.py`'s `loggable`.
- **Nothing phones home.** No telemetry, no hosted relay. The only traffic is
  to the platforms and to the deployment. Teams is the exception the platform
  imposes: its traffic always passes through Azure Bot Service.
- **Where the platform keeps a copy**, the conversation leaves the deployment:
  Slack and Telegram store the messages they carried. That belongs in
  `privacy.md` once connectors are real.

## 12. Open questions

- One agent per connector process (`ROBINAUTS_AGENT_ID`), or an agent per
  platform, per channel, or chosen by a command?
- Shared threads: one conversation per person (today), or one per thread
  owned by whoever started it, once shared projects exist?
- Linking a platform account to a person who signs in on the web, so that
  their Slack conversations appear in their web history.
- Attachments: images and files sent to the bot, and files the agent returns.
- Stopping a turn from the platform (a reaction, a button), and tools that ask
  the person before running, which Slack and Telegram can render as buttons.
- Thread links in the store, keyed by connector and platform thread.
- The endpoint's vocabulary: the proof of concept says "channels"
  (`/api/channels/…`, `ROBINAUTS_CHANNELS_SECRET`). "Connectors" is the word
  of this work; one of the two should go.

## 13. Sources

- CopilotKit, [Introducing Channels SDK](https://www.copilotkit.ai/blog/channels-sdk);
  [Channels SDK on GitHub](https://github.com/CopilotKit/channels-sdk);
  [Direct provider adapters](https://docs.copilotkit.ai/reference/channels/sdk/direct-adapters);
  [Channel reference](https://docs.copilotkit.ai/reference/channels/classes/Channel);
  [Slack](https://docs.copilotkit.ai/slack);
  [OpenTag starter](https://github.com/CopilotKit/OpenTag);
  [Telemetry](https://docs.copilotkit.ai/telemetry).
- OpenClaw, [CLI backends](https://docs.openclaw.ai/gateway/cli-backends);
  [CLI backend plugins](https://docs.openclaw.ai/plugins/cli-backend-plugins);
  [ACP agents](https://docs.openclaw.ai/tools/acp-agents);
  [Plugin SDK](https://docs.openclaw.ai/plugins/sdk-overview);
  [Gateway security](https://docs.openclaw.ai/gateway/security);
  [OpenResponses backend (Hugging Face blog)](https://huggingface.co/blog/darielnoel/an-agentic-backend-openclaw-integration).
- Vercel, [Chat SDK adapters](https://chat-sdk.dev/adapters);
  [desplega-ai/chat-py](https://github.com/desplega-ai/chat-py);
  [Nivesh30/chatnec](https://github.com/Nivesh30/chatnec);
  [opsdroid connectors](https://docs.opsdroid.dev/en/v0.30.0/_sources/connectors/index.md.txt);
  [LangBot on PyPI](https://pypi.org/project/langbot/).
- Slack, [chat streaming for AI apps](https://docs.slack.dev/changelog/2025/10/7/chat-streaming);
  [`slack_sdk` ChatStream](https://docs.slack.dev/tools/python-slack-sdk/reference/web/chat_stream.html).
- aiogram, [sendMessageDraft](https://docs.aiogram.dev/en/latest/api/methods/send_message_draft.html).
- The package metadata quoted (versions, licences, dependencies) was read
  from the npm and PyPI registries on the dates above.
