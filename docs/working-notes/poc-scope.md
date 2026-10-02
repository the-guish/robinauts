# POC scope

Status: draft, for discussion. The specs in [../specs/](../specs/core.md)
describe the whole platform; this note cuts the smallest thing worth
deploying out of them. Where the POC does less than the specs, it says so;
it never does something the specs forbid, so nothing built here has to be
thrown away.

## What the POC is

A deployed Robinauts that a handful of colleagues can sign in to with
Google or Okta and chat with an agent. Each person sees their own
conversations and nobody else's. Model API keys come from the environment.

It proves, end to end: sign-in, the seams (chat UI, agent engine, wire),
the platform-owned conversation record, runs that survive a dropped
request, and a deployment that is one wheel plus one PostgreSQL.

## In

**Sign-in** ([sign-in.md](../specs/sign-in.md))

- Google and Okta over OpenID Connect, the full flow as specified: state,
  nonce, PKCE, claim checks, allow list, server-side sessions with the
  `__Host-` cookie, origin checks on writes, sign-out.
- A user record created at first sign-in.
- The sign-in page, the error codes, return-to after sign-in.
- The TOML configuration, secrets by environment variable name.
- The local development mode: no sign-in, one fixed local user, loopback
  only, with its warning and banner.

**Conversations**

- Private to their author. Every route checks ownership; there is no other
  way to see a conversation.
- Text only.
- Stored in the platform's own format, as a tree (every message has a
  parent), with what an answer records: agent, engine, model, run.
- Edit and regenerate, with branches, as far as assistant-ui provides them
  over our API. First thing to cut if it costs more than expected; the
  tree stays in the store either way.
- List in the panel, open, rename, delete. The title is the beginning of
  the first message.

**Agents and models**

- Agents defined in the configuration; a picker on the empty chat when
  there is more than one.
- Provider keys read from the environment at start-up, by the variable
  names the configuration gives. Never stored, logged or sent to the
  browser.
- Providers: Anthropic and OpenAI. OpenRouter comes with the
  OpenAI-compatible client.
- **Both engines**, LangGraph and Pydantic AI, behind the agent port, with
  the shared contract suite and the swap test. This is the seam the
  project exists to prove. Second thing to cut: if time is short, one
  engine ships first and the other follows, but the port and the contract
  suite are written for two from the start.
- Tracing to LangSmith and Logfire forced off.

**Runs and the wire** ([wire.md](../specs/wire.md))

- A message starts a run; the run executes in the background, persists
  messages as they are produced, and survives a dropped request.
- One active run per conversation; cancel.
- AG-UI over SSE, emitted by the `api` layer; re-attach to an active run
  when a conversation is opened.
- On restart, runs left `running` are marked `interrupted`. Retry is
  sending the message again.
- A timeout per model call and per run.

**Interface** ([frontend.md](../specs/frontend.md))

- The neorc shell: the collapsible panel with "new chat", the history and
  the profile block with sign-out; the rail state remembered.
- The chat on assistant-ui with Tailwind, behind the `src/chat/` seam, with
  the lint rule that confines it.
- Dark mode following the operating system.

**Backend and deployment** ([operations.md](../specs/operations.md))

- The layout and its contracts, as scaffolded. FastAPI, asyncpg,
  hand-written SQL.
- One schema definition, edited in place, applied by `robinauts db init`;
  no incremental migrations. Refusal to start on a schema mismatch.
- `robinauts start`. A health endpoint.
- One wheel containing the built frontend, built in CI.
- A deployment guide: PostgreSQL, the wheel, the configuration, the
  environment variables, a TLS-terminating reverse proxy in front,
  registering the redirect URI with Google and Okta. Tried for real on one
  machine.

**Open source, from day zero** ([open-source.md](../specs/open-source.md))

- Headers, `NOTICE`, `AUTHORS`, `CONTRIBUTING.md` with the DCO and the AI
  section, `DEPENDENCIES.md`, `docs/legal/ip-clearance.md` and
  `third-party.md` with their first entries (neorc sign-in
  design, vendored assistant-ui components).
- CI, blocking: tests with the architecture contracts, ruff, black; the
  Python licence gate and `pip-audit`; the frontend lint, type check, tests
  and build with the bundle licence gate, `npm audit`; `reuse lint`; the
  DCO check.
- Sign-off on every commit.

## Out

Stated by the scope: **roles** (no admin; nobody sees anybody else's
anything), **projects**, **forking**, **attachments** and images.

Also out, all specified and all addable later without rework:

- share links;
- search, export, archive;
- the trash: delete removes the conversation for good in the POC;
- generated titles;
- reasoning content (dropped if a model emits it; the format has the place
  for it);
- vendor-specific extras kept across turns;
- retention, purge, audit log;
- operator limits other than the timeouts;
- Gemini and Bedrock;
- ~~the responsive drawer and the theme toggle~~ — built in step 18 after
  all: both were cheap beside the shell, and the interface spec asks for
  them;
- several backend processes (one process; events need not travel through
  the database yet, but the run and its messages are in the database);
- draining runs on shutdown;
- the release workflow, trusted publishing, SBOM, signed tags — the wheel
  is built in CI and installed from the artifact;
- everything the specs already call planned: tools, memories, usage
  reporting, API tokens, other channels, the container image.

## Done when

1. Two people sign in, one through Google and one through Okta, on a
   deployed instance over https. A third person, not on the allow list, is
   refused.
2. Each sees only their own conversations; requesting another person's
   conversation by id is refused.
3. A conversation is held with an agent on one engine and one vendor; the
   agent's engine and model are changed in the configuration; after a
   restart the same conversation continues on the other engine and vendor.
4. A tab is closed in the middle of a long answer; on reopening the
   conversation, the answer is complete, or still arriving and the stream
   re-attaches.
5. With the API keys removed from the environment, start-up fails with a
   message naming the missing variables. The keys appear in no log, no
   response and no database row.
6. A clean machine goes from nothing to a running instance by following
   the deployment guide alone.
7. CI is green with every gate in place, and adding a GPL dependency makes
   it red.

## Order of work

Each step is one branch, `feature/poc-<n>-<name>`, stacked on the previous
one, built by an implementer agent, reviewed by a fresh reviewer agent
until clean, and landed by the driver as one signed-off commit. The process
is the three-agent recipe (`recipes/three-agent-steps.md`, beside this
repository); each step's summary and review counts are recorded in
[poc-progress.md](poc-progress.md).

A step aims at under a thousand lines of diff and one concern, so that a
review is quick and its findings are about one thing.

**Groundwork**

0. `poc-0-documents` — Repository documents: `NOTICE`, `AUTHORS`, `CONTRIBUTING.md`,
   `DEPENDENCIES.md`, `docs/legal/*`, `REUSE.toml`.
1. `poc-1-ci-python` — CI for Python: tests with the architecture contracts, ruff, black, the
   licence gate, `pip-audit`, `reuse lint`, the DCO check.

**Sign-in, from the core outward**

2. `poc-2-signin-core` — Domain and core for sign-in: identity, the allow-list matchers, the
   claim checks, configuration validation.
3. `poc-3-signin-application` — Ports and fakes for sign-in, and the application flow: begin, callback,
   session, sign-out, user creation.
4. `poc-4-credential-store` — The schema definition and the credential store on asyncpg, with its
   contract suite.
5. `poc-5-oidc-adapter` — The OIDC adapter, the configuration reader, the stand-in provider.
6. `poc-6-auth-api` — `api`: the auth routes, cookies, origin checks, the permission
   declaration and its test, `create_app`, the health endpoint.
7. `poc-7-dev-mode` — The local development mode.

**Conversations and runs**

8. `poc-8-conversation-format` — Domain and core for the conversation format: the tree, the kinds of
   content, what an answer records.
9. `poc-9-conversations-application` — Conversation and run ports, fakes, and the application for
    conversations: list, open, rename, delete, ownership.
10. `poc-10-run-lifecycle` — The run lifecycle in the application, against a fake agent: start,
    persist as produced, one active run, cancel, interrupted.
11. `poc-11-conversation-stores` — The conversation and run stores on asyncpg, with their contract
    suites.
12. `poc-12-run-executor` — The run executor adapter: tasks, the registry, the lifespan, timeouts.
13. `poc-13-conversations-api` — `api`: the conversation routes, the OpenAPI snapshot.
14. `poc-14-agui-stream` — `api`: the AG-UI stream, re-attaching.

**Engines**

15. `poc-15-first-engine` — The agent contract suite and the first engine; provider
    configuration, keys from the environment.
16. `poc-16-second-engine` — The second engine, and the swap test.

**Frontend**

17. `poc-17-frontend-skeleton` — Skeleton and CI for JavaScript: Vite, TypeScript, ESLint with the rule
    that confines assistant-ui, the bundle licence gate,
    `bundled-packages.txt`, `npm audit`.
18. `poc-18-shell` — The shell: tokens, the Tailwind theme, the panel and its rail, the
    profile block, session handling, the sign-in page, the banner of the
    local development mode.
19. `poc-19-history` — History in the panel, routing, conversation management against the
    API.
20. `poc-20-vendor-assistant-ui` — The vendored assistant-ui components: only the copy, its README, the
    MIT text and the `ip-clearance` entry. A large diff, and a mechanical
    one: the review is about provenance.
21. `poc-21-chat` — The chat behind `src/chat/`: the AG-UI client, streaming, re-attaching,
    cancel, the agent picker; edit and regenerate if they come cheaply.

**Delivery**

22. `poc-22-wheel` — The frontend in the wheel, `robinauts start` and `robinauts db init`,
    the wheel as a CI artifact.
23. `poc-23-deployment` — The deployment guide and the real deployment, with what broke written
    down.

**What this order gives**

- After step 7, signing in works end to end, before any chat code exists.
- After step 14, the backend is complete and testable with a fake agent.
- Steps 17 to 19 depend only on steps 6 and 13, so the frontend can
  proceed in parallel with steps 8 to 16.
- Steps 0 and 8 are implemented by the driver itself: what they record was
  settled in discussion, not in the repository.

## To settle before starting

- Where the POC is deployed, and under which host name — the redirect
  URIs registered with Google and Okta depend on it.
- The Google and Okta client registrations to use.
