# ADR 0008 — A shell of apps, in a repository of its own

- Status: proposed.
- Date: 2026-11-14
- Study: [docs/research/app-shell/STUDY.md](../research/app-shell/STUDY.md).
  The evidence, the comparisons and the file references are there.

## Context

Robinauts embeds neorc. The manager is mounted at `/neorc` and its UI is
framed with `?embedded`. Robinauts' left panel draws neorc's navigation
again, by hand (`frontend/src/flows/FlowsSection.tsx`,
`frontend/src/flows/orchestrator.ts`). The copy lags: neorc's Queues page is
missing. More apps are planned, some optional. The person should switch
between apps, and the active app should own the left, central and right
panels.

neorc is served with no authentication when embedded, so a flows folder is
refused when sign-in is on
([orchestration.md](../architecture/orchestration.md)).

Robinauts' sign-in is neorc's design written again
([sign-in.md](../specs/sign-in.md); `docs/legal/ip-clearance.md`,
2026-09-21). The two copies already drift. Both keep a fixed table from
roles to permissions in code and check one permission per route. Robinauts
has personal tokens; neorc has service tokens bound to one queue.

## Decision

**Robinauts becomes a shell that hosts apps. The shell, sign-in, sessions,
API tokens, roles and the app registry go into a third repository, a
library both projects import. It is built in Robinauts first and cut out
once it works.**

- **The shell** owns an app rail, a top bar, sign-in and the session, the
  theme, and routing as `#/<app>/<app route>`. It owns no app panel.
- **An app is one same-origin frame** that draws its own three panels.
  Chat is app 1. neorc is app 2. Not one frame per panel, not web
  components, not module federation: frames keep each app's build,
  dependencies and policy its own, and neorc already fits.
- **Apps are trusted code.** A same-origin frame isolates styles and
  crashes, not privileges. Only Python packages the operator installed are
  apps. Third-party UI stays sandboxed, as MCP views are
  ([ADR 0007](0007-mcp-apps-in-the-side-panel.md)).
- **Apps plug in on the server.** A Python package registers through an
  entry point. It gives a manifest (id, title, icon, entry, optional,
  required role, permissions, manifest version), an ASGI app mounted at
  `/apps/<id>/`, its built UI, and a lifespan. `GET /auth/session` lists the
  apps the person may see. An app not installed has no tab.
- **The shell protocol** is JSON-RPC 2.0 over `postMessage`, versioned in
  its handshake, shaped like MCP Apps: `shell/initialize`,
  `shell/initialized`, `shell/navigate`, `shell/open`, `shell/theme`,
  `shell/title`, `shell/badge`, `shell/layout`, `shell/key`,
  `shell/session-ended`. Both sides check the origin and the source window.
- **Identity.** The library authenticates every request once, for every
  mount, and puts one principal in the ASGI scope. An app in another
  process gets a short-lived assertion signed with HMAC from the standard
  library. A trusted header from an operator's proxy is an optional
  authenticator. No JWT or crypto dependency.
- **Roles.** Plain roles, namespaced by app. Each app declares its
  permissions and its fixed role table. A grant is a role of an app,
  optionally on one resource (`neorc:responder@queue:approvals`). People
  get grants from the configuration, with the allow list's matchers.
  Service tokens carry theirs. Resource checks stay in the app. No policy
  engine.
- **What stays in each app.** Robinauts keeps its `users` row, its change
  feed and device pairing. neorc keeps `neorc_core` free of the library and
  maps the principal to its own.
- **Packaging.** A pure core package and an I/O package in Python. The
  shell UI is built in the library's CI and shipped in its wheel, as
  `neorc-ui` ships neorc's UI, so Robinauts' runtime closure stays `react`
  and `react-dom` ([ADR 0006](0006-chat-ui-on-react-alone.md)). The protocol
  client is copied into each app, with an ip-clearance entry, until a third
  app justifies an npm release with provenance. One version per repository,
  from a tag. PyPI with trusted publishing; a commit pin until then.
- **The database.** The library's tables live in a schema of their own,
  `shell`, prefixed `shell_`, in the host's one PostgreSQL. neorc standalone
  keeps them in its own database.
- **Composed, and kept.** The OIDC client on `httpx`, the server-side
  sessions and the plain role tables are kept: they exist and are tested.
  Authlib (BSD-3-Clause) and pycasbin (Apache-2.0) are the named fallbacks,
  for signature verification and for policies as data. Identity proxies
  and authorization services are refused as requirements: each is a second
  process.

## What changes in earlier decisions

- **ADR 0007.** The right panel moves into the Chat app, unchanged. Where
  ADR 0007 says "the interface", read "the Chat app". The view's URL,
  policy, sandbox and bridge stay. The view's frame now nests inside the
  Chat app's frame. The Chat app's page keeps `frame-src 'self'`.
- **ADR 0006.** Unchanged. The chat's seam stays. The chat is now drawn in
  the Chat app's page rather than in the whole interface.

## Order

The study's migration (section 9) in short. Each step leaves both projects
working.

1. Robinauts: the protocol, shared with the MCP bridge; neorc framed with
   it.
2. neorc: a protocol client, a configurable `frame-ancestors`, a theme
   override.
3. neorc: an authenticator port in `create_app`, option sources for forms,
   grants on one queue for people.
4. Robinauts: its principal handed to neorc; flows allowed with sign-in on.
5. Robinauts: the rail, the top bar, Chat and neorc as apps. The Flows
   mirror is deleted.
6. Robinauts and neorc: run-start forms move to neorc, with Robinauts'
   models, agents and repositories as option sources. `NewRunPage` and
   `/api/flows` are deleted.
7. Robinauts: the registry and the roles.
8. The third repository is cut out of Robinauts.
9. neorc standalone adopts it.
10. Both publish to PyPI; the git pins go.

## Alternatives

- **Keep everything in Robinauts.** Cheapest now. The two sign-ins stay
  two, and neorc standalone gets no shell. It is the first half of the
  chosen order, not its end.
- **Put the shell in neorc.** It would make Robinauts depend on neorc for
  sign-in, and neorc could no longer be discarded from Robinauts.
- **An identity proxy in front of both.** A second process, against goal 6.
  It authenticates; it draws no shell and knows no queue or owner, so
  tokens and roles stay in each app anyway.

## Consequences

- The Flows mirror goes, and new neorc pages appear without work in
  Robinauts.
- Flows work with sign-in on.
- A third repository to keep, with the same gates as the other two: the
  licence gates, the audits, the bundled-package list, DCO and REUSE, from
  its first commit.
- A change across the library and an app is two pull requests.
- The end-to-end suite reaches the chat inside a frame.
- Sessions and tokens must survive the move of the credentials tables, or
  the release says that everyone signs in again.
- neorc's steps (2, 3, 6, 9) are its maintainers' to accept.

## Open

- The names of the repository and its packages.
- Whether Robinauts' `/api/` stays at the root.
- One personal token for every app, or one per app.
- The cluster case's assertion, sketched and not specified.
- Device pairing in the library, or Robinauts' alone.
