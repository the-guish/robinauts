# Robinauts as a shell of apps: a study

- Status: study. It decides nothing. The decision it argues for is the
  draft [ADR 0008](../../adr/0008-a-shell-of-apps-in-a-repository-of-its-own.md).
- Date: 2026-11-14.
- Read at: Robinauts `main` at `e2f276d`; neorc `main` at `26a30a0`, the
  commit Robinauts pins (`backend/pyproject.toml`, `[tool.uv.sources]`).
- Paths that start `neorc:` are in
  [open-shipyard/neorc](https://github.com/open-shipyard/neorc). Every
  other path is in this repository.

## Contents

1. [The question](#1-the-question)
2. [What exists today](#2-what-exists-today)
3. [Where the shell lives: four options](#3-where-the-shell-lives-four-options)
4. [One model of identity and roles for both](#4-one-model-of-identity-and-roles-for-both)
5. [Packaging, releases and the database](#5-packaging-releases-and-the-database)
6. [What to compose before building](#6-what-to-compose-before-building)
7. [The apps, the frames and the protocol](#7-the-apps-the-frames-and-the-protocol)
8. [Run-start forms and options from another app](#8-run-start-forms-and-options-from-another-app)
9. [Migration](#9-migration)
10. [Risks and open questions](#10-risks-and-open-questions)
11. [Recommendation](#11-recommendation)

## 1. The question

Robinauts embeds neorc in `robinauts start`. The manager is mounted at
`/neorc`. Its UI is framed with `?embedded`. Robinauts' left panel draws
neorc's navigation again, by hand
([orchestration.md](../../architecture/orchestration.md), "The Flows pages";
`frontend/src/flows/FlowsSection.tsx`, `frontend/src/flows/orchestrator.ts`).

That copy already lags. neorc's sidebar has a **Queues** entry
(`neorc:ts/neorc-ui/src/components/Layout.tsx:15`, routes `#/queues` and
`#/queues/{q}` in `neorc:ts/neorc-ui/src/router.ts`). Robinauts' Flows tab
has New run, Runs, flows and recent runs, and no queues
(`frontend/src/router.ts`, the `FlowsView` type). Every new neorc page is
work in Robinauts. Every new app would be more.

More apps are planned, some optional. The developer wants the person to
switch between apps, and the active app to own the left, central and right
panels.

The proposal under study:

- Robinauts becomes a **shell**: an app rail, a top bar, sign-in and the
  session, the theme, and routing as `#/<app>/<app route>`. Chat is app 1.
  neorc is app 2.
- An **app** is one same-origin frame that draws its own three panels.
- Apps **plug in on the server**: a Python package, found by an entry
  point, gives an ASGI app mounted at `/apps/<id>/`, its built UI, and a
  manifest. `GET /auth/session` lists the installed apps.
- A small, versioned **shell protocol** over `postMessage`.
- The shell's sign-in **protects every mount** and hands each app an
  identity it trusts.
- Whether the shell, SSO, sessions, API tokens and RBAC belong in a **third
  repository**, which Robinauts and neorc both import.

## 2. What exists today

### 2.1 The two sign-ins are one design, written twice

Robinauts' sign-in is neorc's design, re-implemented. The spec says so
([sign-in.md](../../specs/sign-in.md), first paragraph). The copy is
recorded in [ip-clearance.md](../../legal/ip-clearance.md), entry
"2026-09-21 — sign-in design, from neorc".

| | Robinauts | neorc |
|---|---|---|
| OIDC client | own code on `httpx`: `backend/src/robinauts/web/oidc.py` | own code on `httpx`: `neorc:python/neorc/src/neorc/auth/_oidc.py` |
| Flow | code flow, `state`, `nonce`, PKCE S256 | the same |
| ID token | claims checked, signature not verified (OIDC Core 3.1.3.7) | the same (`_oidc.py:166-204`) |
| Providers | Google and Okta, one generic client | the same |
| Allow list | six matchers (`web/sign_in.py:66`, `Matcher`) | the same six (`neorc:python/neorc-core/src/neorc_core/_access.py:255-327`) |
| Allow list checked | at sign-in, and again on every use at most once a minute | at sign-in only |
| Configuration | `ROBINAUTS_CONFIG`, TOML, with the model tables | `NEORC_AUTH_CONFIG` or `--auth-config`, TOML (`neorc:.../auth/_config.py`) |
| `public_url` | mandatory, origin root | mandatory, a bare origin; a path is refused (`_config.py:348-353`) |
| User record | a `users` row, keyed by `(provider, subject)` (`controller/adapters/postgres/schema.sql`, `users`) | none: "a principal is what its provider said" (`neorc:docs/working-notes/sso-plan.md`) |
| Session | a row; the cookie holds a 256-bit secret, the row its SHA-256 (`user_sessions`) | the same (`neorc_sessions`), with the principal stored in the row |
| Cookie | `__Host-` prefix, `HttpOnly`, `SameSite=Lax`, `Secure` | `__Host-neorc_session`, the same flags (`neorc:.../manager/_sign_in.py:370-394`) |
| Lifetime | `session_hours`, 12 | `session_hours`, 12 |
| Pending sign-ins | a row per `state` hash, 10 minutes, capped (`pending_logins`) | the same (`neorc_pending_logins`), cap 10,000 |
| CSRF | none; JSON only, `Sec-Fetch-Site`, `Origin == public_url` | the same (`neorc:.../manager/_access.py:544-557`) |
| Local mode | `--dev-no-sign-in`: one real local user, loopback only, `Host` checked | `--no-auth`: no principal, everything allowed, a warning strip |

The two copies already differ in small ways: the per-use allow-list check,
the user row, the local mode. Each fix lands twice or drifts. That is the
strongest reason for a shared library, and it exists today, before any
shell.

### 2.2 Tokens

| | Robinauts | neorc |
|---|---|---|
| Who mints | the person, from the browser, with a session younger than ten minutes (`POST /auth/tokens`) | the operator, on the command line, straight to the database (`neorc tokens create NAME --role R [--queue Q]`) |
| Owner | a user | nobody: the token is its own principal |
| Prefix | `rbt_` (API token), `rbd_` (device session) | `neorc_` |
| Stored | SHA-256 (`api_tokens`, `device_sessions`) | SHA-256 (`neorc_api_tokens`) |
| Lifetime | 90 days, not renewed | optional expiry |
| Scope | everything the owner may do | one role, and for `worker` and `responder` one queue, checked by a database constraint (`neorc:python/neorc/src/neorc/postgres/_schema.py:228-240`) |

The two are different kinds. Robinauts has **personal tokens**: a person's
reach, for their scripts and phones. neorc has **service tokens**: a fixed,
narrow role for a machine. A shared library needs both.

Device pairing (`docs/architecture/device-pairing.md`) is Robinauts' only.
It is the phone channel of the chat. It stays in Robinauts at first
(section 9, step 7).

### 2.3 Roles

Robinauts has **no roles yet**. Two are specified, user and admin, and a
configuration with an `admin` table is refused at start-up
([sign-in.md](../../specs/sign-in.md), "Roles"). The spec already says:
roles map to permissions in one fixed table in `core`; every route declares
its permission; a test asserts it; ownership is checked in the application.

neorc has **six fixed roles and fifteen permissions**
(`neorc:python/neorc-core/src/neorc_core/_access.py:62-156`):
`worker`, `responder`, `scheduler`, `ci`, `user`, `external-trigger`.
"Fixed: roles are not edited." A signed-in person is always `user`. There
is no admin. `worker` and `responder` tokens are bound to one queue. Routes
are guarded by two FastAPI dependencies: `guard` on the whole router, and
`permit(Permission.X)` on each route, which asks `authorize(principal,
permission, queue=...)` (`neorc:python/neorc/src/neorc/manager/_access.py:66-120`).

Both projects chose the same shape: **a fixed role-to-permission table in
code, a check per route, resource ownership in the application**. That
shape is what the shared library should keep.

### 2.4 How neorc is embedded today

- `backend/src/robinauts/orchestrator/adapters/neorc/embedded.py` builds
  neorc's manager with `neorc.manager.create_app`, mounts it at `/neorc`,
  runs the scheduler in the process and one worker in a process of its
  own.
- neorc is served **with no authentication** (`access=None`). So a flows
  folder is refused when sign-in is on
  ([orchestration.md](../../architecture/orchestration.md), "With sign-in
  on, a flows folder is refused for now").
- neorc's UI answers carry `frame-ancestors 'none'`
  (`neorc:python/neorc/src/neorc/manager/_ui.py:38`). Robinauts rewrites the
  header to `'self'` in a wrapper (`Framable`, `embedded.py:199`).
- The frame and the shell talk by **reading each other's `location`**, since
  they share an origin (`frontend/src/flows/FlowsPage.tsx`: the shell
  replaces the frame's hash, and listens to the frame's `hashchange`).
  There is no `postMessage` in neorc
  (`neorc:ts/neorc-ui/src/embedding.ts` reads `?embedded` and nothing else).
- `create_app` has **no hook for an identity from the host**. `guard`
  knows a bearer and its own session cookie. With `access=None` it allows
  everything. A host can override `guard` through FastAPI's
  `app.dependency_overrides`, since `permit` reaches it through the
  module-level `Guarded` alias (`_access.py:96`). That works, and it is not a
  documented interface. It is a stopgap at most (section 9, step 2).
- neorc's sign-in assumes the origin root: `public_url` refuses a path, the
  cookie is `__Host-` with `Path=/`, redirects go to `{public_url}/ui/`.
  Serving it under a prefix is listed as out of scope
  (`neorc:docs/working-notes/sso-plan.md:544-546`). An embedded neorc must
  therefore never run its own sign-in. That matches the requirement.

### 2.5 Packaging today

- neorc is three distributions in one uv workspace: `neorc-core`, `neorc`
  (extras `http`, `manager`, `postgres`) and `neorc-ui` (the built UI as
  package data, built by a hatch hook running `npm ci && npm run build`:
  `neorc:python/neorc-ui/hatch_build.py`). One version for all three, from a
  `v*` tag, by hatch-vcs.
- neorc has a release workflow for PyPI with trusted publishing
  (`neorc:.github/workflows/release.yml`) and has never released: its
  CHANGELOG has only `[Unreleased]`.
- Robinauts builds neorc's three wheels from a pinned commit
  (`backend/pyproject.toml`, `[tool.uv.sources]`, `no-binary-package`;
  `scripts/neorc-from-source.sh`; the "Installing neorc" section of
  [orchestration.md](../../architecture/orchestration.md)). The build needs
  Node.js.
- Robinauts already **ships a built UI it does not build itself**: neorc's,
  inside the `neorc-ui` wheel. That is the precedent for a shell UI shipped
  in a wheel (section 5).

## 3. Where the shell lives: four options

The parts in question: the **shell UI** (rail, top bar, sign-in page,
theme, routing, frames), **SSO** (OIDC sign-in), **the session**, **API
tokens**, **RBAC**, and the **app registry** (entry points, mounts,
manifests).

### Option A — keep everything in Robinauts

Robinauts grows the shell. neorc keeps its own sign-in for standalone use.

- For: no new repository, no new release train. Robinauts' gates cover
  everything. The fastest path to the shell.
- Against: the duplicated sign-in of section 2.1 stays duplicated, and
  grows RBAC twice. neorc standalone gets no shell, so a person who uses
  both sees two interfaces and two sign-ins. A third app that wants to run
  standalone would copy the code a third time, or depend on Robinauts and
  its agent frameworks.
- Discard test: removing the shell means rewriting Robinauts' frontend.
  There is no seam to test.

### Option B — put the shell in neorc

neorc owns the shell. Robinauts' chat plugs into neorc.

- For: neorc already has the sign-in and the roles, and a UI.
- Against: it inverts the projects. neorc is "a lightweight orchestration
  layer with minimal dependencies" ([orchestration.md](../../architecture/orchestration.md),
  first section). A shell for unrelated apps is not orchestration. Robinauts
  would depend on neorc for its sign-in even with no flows, against the
  rule that taking neorc out is taking out one package
  ([orchestration.md](../../architecture/orchestration.md), "Where neorc may
  be imported"; [rules.md](../../architecture/rules.md)). The extra workers'
  images leave neorc out today, and could not.
- Discard test: fails. neorc could not be discarded from Robinauts.

### Option C — an identity proxy in front of both

oauth2-proxy, Pomerium, Authelia or Keycloak signs people in, and passes a
header to Robinauts and neorc.

- For: sign-in becomes configuration. One login for every app on the host.
- Against: a second process, against goal 6, "one frontend, one backend,
  one database" ([core.md](../../specs/core.md)). A proxy authenticates. It
  does not know a neorc queue, a Robinauts conversation owner, or an API
  token minted from the browser, so tokens and RBAC stay in each app anyway.
  It draws no shell. The desktop and local modes would need it too, or
  need a second code path. Some candidates add their own user database and
  portal (Authelia, Keycloak, Ory). Pomerium's builds reportedly moved to a
  licence with usage reporting (section 6), against goals 1 and 3.
- A proxy is still a valid **deployment choice** for an operator who has
  one. The library can accept an identity from a trusted header as one of
  its authenticators (section 4.4). It should not require one.
- Discard test: passes for the proxy itself, but the shell and RBAC remain
  unsolved.

### Option D — a third repository

A library for the shell, sign-in, session, tokens, RBAC and the app
registry. Robinauts imports it and hosts Chat, neorc and later apps. neorc
imports it when it runs standalone. Embedded in Robinauts, neorc uses the
host's identity and signs nobody in.

- For: one sign-in, written once, replacing two copies. One model of roles
  for every app. neorc standalone and Robinauts look and behave alike. A new
  app depends on the library, not on Robinauts. Still one process and one
  database: the library runs inside the host's backend (goal 6).
- Against: a third repository, with its own release train, gates, CI and
  versioning. Changes that span the library and an app take two pull
  requests. A library designed from two consumers may still be wrong for
  the third.
- Discard test: Robinauts reaches the library through one composition
  module and one web middleware (section 9). Removing it breaks those two
  places, and what they wire must be written again. neorc keeps a
  "no shell" mode, so it never depends on the library to run its core.

### Weighed against the goals

| goal ([core.md](../../specs/core.md)) | A: in Robinauts | B: in neorc | C: a proxy | D: a third repo |
|---|---|---|---|---|
| 1. Apache-2.0, gated | yes | yes | depends on the proxy | yes, if the repo carries the same gates |
| 2. Secure, corporate | yes, twice | yes | yes, plus RBAC elsewhere | yes, once |
| 3. Nothing leaves | yes | yes | depends on the proxy | yes |
| 4. Compose over build | builds the shell in place | builds it in the wrong place | composes sign-in only | builds the shell once, composes OIDC and RBAC parts where they fit |
| 6. One backend, one database | yes | yes | **no**: a second process | yes: a library in the host |
| Swappable, discard test | no seam | fails | passes | passes, with a seam to enforce |
| Cost now | lowest | high | medium | highest |
| Cost of the second and third app | high | high | medium | lowest |

**D is the right destination. A is the right first step.** The library
should be cut out of working code in Robinauts, not designed ahead of it.
Section 9 gives the order.

## 4. One model of identity and roles for both

### 4.1 What the library owns, and what stays in the apps

| part | owner | why |
|---|---|---|
| OIDC client, allow list, pending sign-ins | library | identical in both today |
| Identities: `(provider, subject)`, name, email, hosted domain, groups | library | both need them; Robinauts already stores them |
| Browser sessions | library | identical in both today |
| Personal API tokens | library | Robinauts' kind |
| Service tokens with a role and a resource | library | neorc's kind |
| Roles, grants, the permission check | library | one model for every app |
| Each app's permission catalogue and role table | **the app**, declared to the library | the app knows its operations |
| Resource checks (whose conversation, which queue) | **the app** | they need the app's data |
| Robinauts' user row and its change feed | Robinauts | conversations and projects reference `users` (`schema.sql`, `sessions.owner_id`) |
| Device pairing | Robinauts, at first | the chat's phone channel |
| The local development mode | library, as a mode | both have one; one implementation |

Robinauts' `users` row stays. The library calls a hook at each sign-in, as
`SignIn.complete` calls `controller.ensure_user` today
(`web/sign_in.py`, `complete`). The library keeps the credentials. The
app keeps the profile and what hangs off it.

### 4.2 Roles and permissions

Plain roles, namespaced by app. No policy language.

- A **permission** is `<app>:<operation>`: `chat:conversations:write`,
  `neorc:runs:start`, `neorc:tasks:complete`, `shell:apps:admin`.
- Each app declares its permissions and a **fixed table** from its roles to
  them, in code, as both projects do today. neorc's `PERMISSIONS`
  (`_access.py:129-156`) becomes neorc's declaration, with its names
  prefixed.
- A **grant** gives a principal a role of an app, optionally **on one
  resource**: `neorc:responder@queue:approvals`. The resource is a string
  the app interprets. neorc's queue binding is the first case. "A flow"
  (`neorc:docs/specs/roles.md`, "In future versions … specific flow
  resources") is the next.
- Grants to **people** come from the configuration, with the allow list's
  matchers. This generalises Robinauts' planned `admin` table:

  ```toml
  [[grant]]
  role = "shell:admin"
  provider = "okta"
  group = "platform-admins"

  [[grant]]
  role = "neorc:user"
  provider = "okta"
  group = "engineering"

  [[grant]]
  role = "neorc:responder"
  resource = "queue:approvals"
  provider = "okta"
  group = "approvers"
  ```

- Grants to **service tokens** are stored with the token, as neorc stores
  `role` and `queue` today.
- A **personal token** carries its owner's grants, as Robinauts' tokens do.
  Scopes that narrow it are a later step, and fit the same model: a token
  holds a subset of its owner's grants.
- The library decides grants at sign-in and on each use, at most once a
  minute, as Robinauts re-checks the allow list today
  ([sign-in.md](../../specs/sign-in.md), "Known limits").
- **An app is listed only to those who hold a role of it.** The manifest's
  `required_role` is that rule written down.
- Each route declares its permission. A test asserts that every route
  does. Robinauts' spec already asks for this; neorc's `permit` already does
  it.

The three cases in the brief:

| case | grant |
|---|---|
| a Robinauts admin | `shell:admin` and `chat:admin`, from a matcher |
| a neorc responder bound to one queue | a service token with `neorc:responder@queue:<q>`; or a person with that grant from a matcher, which neorc cannot express today |
| per-app permissions | an app appears, and its routes answer, only under a grant of that app |

Today a signed-in person in neorc is always `user`, who may complete tasks
of **every** queue. The model lets an operator give a person `responder` on
one queue alone. That is new for neorc, and neorc's `authorize` already
takes a queue, so the check exists.

### 4.3 The principal an app receives

One frozen value, the same in every app:

```text
Principal
  kind:      person | service | local
  id:        the library's id for the identity or the token
  provider, subject, name, email          (people)
  grants:    frozenset of (app, role, resource | None)
  via:       session | personal-token | service-token | device | local
  fresh_at:  when the sign-in happened   (for "a session younger than ten minutes")
```

An app asks `principal.may("neorc:runs:start", resource="queue:x")`. neorc
maps it to its own `Principal` and `authorize` with a few lines, so
`neorc_core` keeps no dependency on the library.

### 4.4 How an app learns who is asking

- **In the host's process** (every app the proposal names): the library's
  middleware authenticates the request once, for every mount, and puts the
  principal in the ASGI scope (`scope["state"]`). The app reads it from
  there. Nothing is signed, because nothing crosses a process. Headers
  that look like an identity are stripped on the way in.
- **In another process** (neorc on a cluster, connected to Robinauts;
  [orchestration.md](../../architecture/orchestration.md), first section):
  the host forwards a short-lived assertion. It is signed with HMAC-SHA256
  over a key both sides are configured with, from the standard library.
  That keeps [sign-in.md](../../specs/sign-in.md)'s rule: no JWT or crypto
  dependency. A JWT, with JWKS, is a decision of its own if a third party
  must verify it.
- **Behind an operator's proxy**: an authenticator that trusts one
  configured header from one configured peer address. Optional, off by
  default.

neorc needs one change for all three: `create_app` accepts an
**authenticator**, a port that turns a request into a principal or a
refusal. Its own token-and-cookie guard becomes one implementation. The
host's is another. `access=None` stays for `--no-auth`.

## 5. Packaging, releases and the database

### 5.1 The packages

Working names; the names are an open question (section 10).

| package | kind | holds | depends on |
|---|---|---|---|
| `shipyard-shell-core` | Python | the principal, grants, permissions, the allow-list matchers, the manifest schema, the configuration parser; pure, no I/O | nothing |
| `shipyard-shell` | Python | the ASGI middleware, the OIDC client on `httpx`, the session and token store (asyncpg and in-process), the app registry, the auth routes, the static shell UI as package data | core, `httpx`, `starlette`; `asyncpg` as an extra |
| `@open-shipyard/shell-protocol` | npm | the protocol's types and a client for an app: handshake, navigate, open, theme, title, badges, layout; zero dependencies | nothing |
| the shell UI | built assets | rail, top bar, sign-in page, theme, frames | `react`, `react-dom` and the protocol, **bundled at build time in the shell repository** |

The core split mirrors neorc's `neorc-core` (pure) and `neorc` (I/O), and
Robinauts' `contract`, `core`, `adapters` layers
([rules.md](../../architecture/rules.md)).

### 5.2 The shell UI ships in the wheel, built

The shell UI is built in the shell repository's CI and shipped as package
data of `shipyard-shell`, as `neorc-ui` ships neorc's UI. So:

- Robinauts' frontend does **not** take the shell as an npm runtime
  dependency. Its runtime closure stays `react` and `react-dom`
  ([ADR 0006](../../adr/0006-chat-ui-on-react-alone.md),
  [frontend/AGENTS.md](../../../frontend/AGENTS.md)).
- Chat becomes an app with its own built page. It talks to the shell
  through the protocol client.
- The shell repository carries the **same JS gates** as Robinauts and neorc
  ([js-dependencies.md](../../contributing/js-dependencies.md)): exact pins,
  `npm ci`, no install scripts, the bundle's licence allowlist,
  `bundled-packages.txt`, a size budget, `npm audit` and `npm audit
  signatures`, the 10-day cooldown. Its Python side carries the licence
  gate and `pip-audit`. It needs DCO and REUSE as both projects have.
  This is real, recurring work: three repositories to keep in step, not
  two.

The protocol client is small. Each app either takes it from npm, which
needs a published release with provenance (`npm audit signatures`; npm
provenance is required of any package first released after April 2023), or
copies it into its tree with an entry in `docs/legal/ip-clearance.md`, as
Robinauts does with lucide's icon data. A git URL in `package.json` carries
no registry signature, so it would fail `npm audit signatures`. **Copying is
the right start**: the file is a few hundred lines with no dependencies,
and copying keeps Robinauts' runtime closure at two packages. Publish to npm
once a third app needs it.

### 5.3 Versioning and releases

- **One version for the whole repository**, from a `v*` tag, by hatch-vcs,
  as neorc does. The Python packages and the shell UI release together.
- **The protocol has a version of its own**, an integer, sent in the
  handshake. The shell refuses an app whose protocol version it does not
  speak, and says so in the app's place. Additions are optional fields and
  new methods. A change of meaning is a new version.
- **The manifest has a version of its own**, for the same reason.
- **Semver for the Python API.** Before 1.0 a minor release may break, and
  the CHANGELOG says how.
- **PyPI with trusted publishing, from the first release.** neorc already
  has the workflow ([section 2.5](#25-packaging-today)). Until then,
  consumers pin a commit in `[tool.uv.sources]`, exactly as Robinauts pins
  neorc. That also means building from source, so the shell repository's
  wheel build must work from a git checkout, Node.js included, as neorc's
  does.
- A git pin is acceptable for a short time and costs every consumer a Node
  build. Publishing is what [open-source.md](../../specs/open-source.md),
  "Releases" already asks of a release: built in CI from a signed tag,
  trusted publishing, an SBOM.

### 5.4 The database

- The library owns its tables and creates them itself, idempotently, as
  both projects do (`schema.sql`; `neorc:python/neorc/src/neorc/postgres/_schema.py`,
  "a create-if-absent step, not a migration tool"). It keeps a
  `schema_version` row of its own, as Robinauts does.
- **Host with one PostgreSQL** (Robinauts): the library's tables live in a
  schema of their own, `shell`, beside Robinauts' tables and neorc's
  `neorc` schema. This is the pattern neorc already follows inside
  Robinauts ([orchestration.md](../../architecture/orchestration.md),
  "storage": schema `neorc`, tables prefixed `neorc_`). Tables are also
  prefixed `shell_`, so a deployment that cannot create schemas still
  works. Goal 6 holds: one database.
- **neorc standalone**: the library's tables in neorc's database, in the
  schema neorc is given (`neorc:python/neorc/src/neorc/postgres/_pool.py:22-44`
  pins `search_path`), with the same prefix. `neorc_sessions`,
  `neorc_pending_logins` and `neorc_api_tokens` go. `neorc manager start
  --create-schema` creates the library's tables too.
- **Robinauts' foreign keys**: `user_sessions`, `api_tokens` and
  `device_sessions` reference `users`. The first two move to the library
  and reference its identities. `users` gains a unique reference to the
  library's identity, and keeps `(provider, subject)` until the move is
  done. The move is a one-time data migration in Robinauts' schema step.
- In process, with no database, the library has an in-process store, as
  both projects do for tests and for the local mode.

## 6. What to compose before building

Versions and dates were read from PyPI, npm and the projects' repositories
on 2026-11-14. Maintenance is judged from releases and README notices.
GitHub activity was not read.

### 6.1 OIDC client

| candidate | licence | latest | verdict |
|---|---|---|---|
| **our own on `httpx`** | Apache-2.0 | in both trees today | **keep**: about 300 lines (`web/oidc.py` is 299), reviewed twice, no crypto dependency |
| Authlib | BSD-3-Clause (a paid licence is optional, support only) | 1.9.5, 2026-11-05 | the best library if JWKS verification is ever needed; brings `cryptography` and `joserfc` |
| joserfc | BSD-3-Clause | 1.8.0, 2026-11-03 | JOSE alone; the right piece if an assertion must be a verified JWT |
| pyoidc, idpyoidc | Apache-2.0 | 2024 | heavy trees, no Starlette glue |
| python-jose | MIT | 3.5.0, 2025-05 | slow, older crypto backends |
| fastapi-users | MIT | 15.0.5 | in maintenance mode; brings passwords and its own users |

Authlib would save the 300 lines and cost a crypto stack in every
consumer. The lines are written and tested. **Keep our own client** and name
Authlib as the move if signature verification becomes required.

### 6.2 Sessions

Starlette's `SessionMiddleware` signs a cookie and stores the session in it.
It cannot revoke a session, and both projects chose server-side sessions on
purpose (`neorc:docs/working-notes/sso-plan.md`: "sign-out … really end[s] a
session"). `starsessions` (MIT, 2.2.1, 2024) has a pluggable store, which we
would still write. The store is the hashed-secret table both projects
already have. **Keep our own.**

### 6.3 RBAC

| candidate | licence | kind | verdict |
|---|---|---|---|
| **plain roles in code** | — | library code | **choose**: both projects already work this way |
| pycasbin | Apache-2.0, 2.8.0 | embeddable | the fallback if policies become data; costs a model language, and no maintained asyncpg adapter |
| OpenFGA | Apache-2.0 | a service | against goal 6 |
| SpiceDB | Apache-2.0 | a service | against goal 6; gRPC client |
| Cerbos | Apache-2.0 | a service | against goal 6 |
| Topaz | Apache-2.0 | a service | against goal 6 |
| Oso (library) | Apache-2.0 | embeddable | archived in favour of a commercial cloud |
| Permify | AGPL-3.0 | a service | forbidden ([open-source.md](../../specs/open-source.md)) |

The grant model of section 4.2 is a role, an app and an optional resource.
That is a set lookup. A policy engine would be a language to learn and a
dependency to vet for a check that fits in a function. The model is shaped
so that Casbin's RBAC-with-domains model could replace the check without a
change to the grants.

### 6.4 Identity proxies

oauth2-proxy (MIT, v7.15.6) is the cleanest: one binary, authentication
only. Authelia, Keycloak and Ory bring their own users, portals and
servers. Pomerium's source is Apache-2.0; the survey for this study read
that its official builds since 2026-11-01 carry a community licence with
usage reporting. That was not checked further and should be before anyone
relies on it. All of them are a second process (section 3, option C).
Support a trusted header as an optional authenticator; require none.

### 6.5 Micro-frontend frameworks

| candidate | licence | iframes | verdict |
|---|---|---|---|
| **our own frame host** | — | yes | **choose** |
| Luigi (SAP) | Apache-2.0, 2.33.0 | yes | the closest fit: iframe-based, a zero-dependency client. Costs: a whole framework with its own navigation and configuration model, not React; the shell would be Luigi's shape |
| single-spa | MIT | no | one document; a third runtime package |
| qiankun | MIT | no | a JS sandbox; `lodash` and more; stalled on 2.x |
| Module Federation (`@module-federation/enhanced`, `/vite`) | MIT | no | shares code at build time; a runtime of its own; `@originjs/vite-plugin-federation` is MulanPSL-2.0, not allowed |
| Piral | MIT | no | its own router and store |

Luigi is worth reading before writing the frame host: its client API
(`linkManager().navigate`, `addContextUpdateListener`, `showAlert`) is a
tested list of what apps ask a shell. Adopting it would trade our React
shell for a framework larger than what we need, and its configuration
model would become the manifest's.

### 6.6 The protocol transport

| candidate | licence | verdict |
|---|---|---|
| **our own, JSON-RPC 2.0 shaped like MCP Apps** | — | **choose**: Robinauts already speaks it (`frontend/src/side/bridge.ts`) |
| penpal | MIT, 8.0.1, zero dependencies | good; a third runtime package for a few hundred lines |
| comlink | Apache-2.0 | aimed at workers |
| post-robot | Apache-2.0 | three dependencies |

### 6.7 MCP Apps as the shell protocol

MCP Apps (SEP-1865) is stable at its 2026-01-26 version
(<https://github.com/modelcontextprotocol/ext-apps>). It is JSON-RPC 2.0
over `postMessage`: a `ui/initialize` handshake that returns the host's
context (theme, styles), `ui/notifications/initialized`, `size-changed`,
`host-context-changed`, `ui/open-link`. Robinauts' side panel already hosts
it ([ADR 0007](../../adr/0007-mcp-apps-in-the-side-panel.md)).

It is not the shell protocol as it stands. It is built around a tool and
its result. A view is pushed as a resource, not a routed app. It has no
navigation, no deep links, no badges. **Borrow its envelope and its
handshake**: one JSON-RPC library in our tree for both, the same theme
fields, and the rule that the host sends nothing before `initialized`. Name
the methods `shell/…`, so the two never collide.

## 7. The apps, the frames and the protocol

### 7.1 One frame per app

Three ways to compose apps in one page:

| | one frame per app | web components | module federation |
|---|---|---|---|
| Builds | independent; each app builds its own bundle | one runtime, shared | one runtime, shared, coupled at build |
| React versions | each its own (neorc pins React 19.0.8; Robinauts its own) | shared or duplicated | must agree |
| Styles | isolated by the document | shadow DOM; tokens must cross it | shared; collisions possible |
| CSP | per document; each app's answer sets its own | one policy for all | one policy for all |
| A crash | stays in its frame | can take the page | can take the page |
| Deep links, Back | need the protocol | native | native |
| Focus, a11y | need care across documents (below) | one document | one document |
| Works with neorc today | **yes**: it is framed now | a rewrite of neorc's UI | a rewrite of neorc's build |
| Dependencies | none | none, or `lit` | about ten `@module-federation/*` packages |

**One frame per app is right.** It is the only option neorc already fits,
and the only one that keeps each app's dependency gates its own.

Same-origin frames give **no security isolation**. An app's script can
reach the shell and every other app, with the person's session. So an app
is **trusted code**: a Python package the operator installed, under the
same supply-chain rules as Robinauts. That is the difference from an MCP
view, which is third-party code, sandboxed without `allow-same-origin`
([ADR 0007](../../adr/0007-mcp-apps-in-the-side-panel.md)). An app from a
third party would need an origin of its own, which goal 6 argues against.
That is out of scope, and the ADR says so.

### 7.2 Frame, not panels

The alternative is the shell framing each panel: three frames per app.
That would make the shell know every app's layout, put three documents
behind one app's state, and need the protocol for every click between an
app's own panels. **One frame that draws its own three panels** keeps an
app whole. The shell owns only the rail and the top bar.

### 7.3 The right panel and ADR 0007

The right panel today holds MCP apps beside a conversation
([ADR 0007](../../adr/0007-mcp-apps-in-the-side-panel.md),
[frontend.md](../../specs/frontend.md), "The right side panel"). It is the
chat's: it follows the conversation, its tabs are remembered per
conversation, and it refreshes on each turn.

Under the proposal, **the right panel moves into the Chat app, unchanged.**
The shell has no right panel. The MCP view's frame nests inside the Chat
app's frame: shell page → Chat (same origin) → MCP view (sandboxed, opaque
origin). Nothing in ADR 0007 changes:

- the view's URL, policy and sandbox stay as they are;
- the Chat app's page must say `frame-src 'self'`, as the interface's does
  today;
- the bridge (`frontend/src/side/bridge.ts`) checks `event.source` against
  its frame's window, which holds at any depth.

What changes is that ADR 0007's "the interface" now means "the Chat app".
The draft ADR records that.

neorc's right side (a run's details, a task's form) is neorc's own, inside
its frame.

### 7.4 The shell protocol

JSON-RPC 2.0 over `postMessage`, between the shell page and each app's
frame. Each side checks `event.origin` equals its own origin and
`event.source` is the expected window. The shell posts to its own origin,
never `"*"`.

| method | direction | what |
|---|---|---|
| `shell/initialize` | app → shell | the app's protocol version and id; answered with the shell's version, the principal's display fields, the theme, the layout class, and the app's route |
| `shell/initialized` | app → shell | the app is ready; the shell shows the frame |
| `shell/navigate` | both | the app's route changed, or the shell asks it to change; `replace` or `push` |
| `shell/open` | app → shell | show something in another app: `{app, route}`; the shell checks the app exists and the person may see it |
| `shell/theme` | shell → app | light or dark, and the token values |
| `shell/title` | app → shell | the document title for the top bar and the tab |
| `shell/badge` | app → shell | a count or a dot on the app's rail icon |
| `shell/layout` | both | the layout class (wide, narrow); the app's drawer opened or closed, so only one drawer is open |
| `shell/key` | app → shell | a shell shortcut pressed inside the frame |
| `shell/session-ended` | app → shell | an API answered 401; the shell shows the sign-in page |

Routing: the shell's address is `#/<app>/<app route>`. The shell loads each
app's frame at `/apps/<id>/ui/?shell=1#<app route>`. Thereafter the route
moves by `shell/navigate` both ways. The shell writes the address bar, so
Back moves the app. Today's code does this by reading the frame's
`location` (`frontend/src/flows/FlowsPage.tsx`). The protocol replaces that,
so an app may one day move to another origin without a change of design.

Panel widths and collapse stay inside the app, remembered by the app. The
shell does not size an app's panels; it tells the app how wide it is.

### 7.5 Accessibility, focus, theme, layout

- **Each frame has a `title`**, the app's name. Inside, the app keeps its
  own landmarks: `nav` for the left panel, `main` for the centre, `aside`
  for the right.
- **Switching apps moves focus** into the new frame's `main`. The app
  answers `shell/navigate` by focusing its main heading.
- **The rail is a `nav` with a list of links**, each with the app's name as
  its accessible name. With the rail collapsed, the name is still the
  accessible name, and a tooltip shows it. Arrow keys move along the rail.
- **Keys pressed inside a frame do not reach the shell.** Shortcuts the
  shell owns are forwarded with `shell/key`. Escape stays the app's.
- **Theme**: the shell owns the light, dark and system choice. It sends the
  choice and the token values. Robinauts' tokens came from neorc
  (`frontend/src/tokens.css`; `neorc:ts/neorc-ui/src/index.css:1-55`) and
  the names have drifted (`--ground`, `--paper`, `--ink` in neorc). The
  library fixes one set of names, and each app maps its own to them once.
  neorc has no way to override its theme today; it reads
  `prefers-color-scheme`. It needs one (section 9).
- **Reduced motion and the focus outline** stay each app's, under the same
  rules as Robinauts' frontend.
- **Narrow screens**: the rail becomes a drawer opened from the top bar,
  as Robinauts' left panel does today
  ([frontend.md](../../specs/frontend.md)). An app's own panels are
  drawers inside its frame. `shell/layout` keeps at most one drawer open.
- **Two scrollbars**: the frame fills the area under the top bar; the shell
  page never scrolls.
- **Print** prints the shell. An app that needs printing opens its page
  alone.

### 7.6 The app registry on the server

- An app is a Python package with an entry point in the group
  `shipyard_shell.apps`. The entry point names a function that returns the
  app: its manifest, its ASGI application, its built UI directory, and a
  lifespan (start and stop).
- The lifespan matters: embedding neorc starts a scheduler and a worker
  process (`embedded.py`). Robinauts' `Orchestrator` contract already has
  mount, start and stop ([orchestration.md](../../architecture/orchestration.md),
  "Where neorc may be imported"). That contract is the shape of an app.
- The manifest: `id`, `title`, `icon` (a name from the shell's icon set; no
  logos), `entry` (the UI's path under the mount), `optional` (a missing
  optional app is skipped; a missing required app refuses the start),
  `required_role`, `permissions` (the catalogue and the role table), and the
  manifest's version.
- The shell mounts each app at `/apps/<id>/`, its API and its UI under it.
  Robinauts' own API stays at `/api/` for its existing clients; the Chat
  app's page is at `/apps/chat/ui/`.
- `GET /auth/session` lists the apps the person may see: id, title, icon,
  entry. An app that is not installed is not listed. The `flows_url` field
  goes.
- **Which apps Robinauts runs** is still the operator's: installed, and
  enabled in the configuration. neorc embedded also needs its flows folder,
  as today.
- The registry is a seam. Entry points are discovery. A host may also pass
  its apps by hand, and tests do.

## 8. Run-start forms and options from another app

Robinauts' New run form offers three lists of values: `repo_branch`,
`model`, `agent` ([orchestration.md](../../architecture/orchestration.md),
"Flows as forms"; `backend/src/robinauts/orchestrator/application/flows.py`;
`frontend/src/flows/NewRunPage.tsx`). neorc's forms cannot: "options from
another service" is left out, with the note "a route of the manager's that
asks it, and its configuration" (`neorc:docs/architecture/forms.md:203`).

The options must be known **on the server**. neorc's manager checks the
inputs of every run start against the form
(`neorc:docs/architecture/forms.md`: "a form is not only how a page looks.
It narrows what the flow accepts."). A list filled in the browser alone
would not be checked.

Proposal:

- neorc gains an **option source** port: given a source name and the
  principal, the values, each with a label.
- A form's `ChoicePicker` names a source instead of listing values:
  `"options": {"source": "chat:models"}`.
- The manager's form route fills the picker from the source. A run start
  checks the value against the same source.
- Standalone, a source is configured by URL, as forms.md foresees, and
  asked with a service token.
- **Embedded, the host registers sources as functions**, in the process.
  Robinauts provides `chat:models`, `chat:agents` and `chat:repo_branch`
  from the code that answers `/api/flows` today. No HTTP, no credential.
- The registry carries them: an app's manifest may **offer** option sources
  and **ask** for them by name. A missing source is a refusal at the start,
  as a form that does not fit its flow is today.

Until neorc has this, Robinauts keeps its own run-start pages. In the shell
they are a page of the Chat app, or of a small Robinauts "flows" app, that
`shell/open`s the run in neorc once started. Section 9 puts the deletion
after neorc's change.

## 9. Migration

Each step leaves both projects working and is one or a few pull requests.
**R** is Robinauts, **N** is neorc, **S** the shell repository.

| # | step | where | what it leaves working |
|---|---|---|---|
| 1 | **The protocol, in Robinauts.** Write the JSON-RPC envelope once, shared with the MCP bridge. Frame neorc with it: `shell/navigate`, `shell/title`. Keep the `location` code as the fallback for a neorc that does not speak it. | R | everything as today |
| 2 | **neorc speaks the protocol.** A small client behind `?embedded`: navigate, title, theme. `frame-ancestors` configurable instead of `'none'`, so Robinauts can drop the `Framable` wrapper. A theme override. | N | neorc standalone unchanged; embedded, the frame reports its moves |
| 3 | **neorc accepts an authenticator.** `create_app(authenticator=...)`, the token-and-cookie guard as one implementation. Option sources (section 8). A person may hold a grant on one queue. | N | neorc standalone unchanged |
| 4 | **Robinauts passes its principal to neorc.** Lift the refusal of a flows folder with sign-in on. Map Robinauts' user to `neorc:user` until grants exist. | R | flows with sign-in on, for the first time |
| 5 | **The shell layout, in Robinauts.** A rail and a top bar. Chat as app 1, in its own frame, with its right panel. neorc as app 2, with its own sidebar shown (`?embedded` keeps the sidebar and drops the brand and profile, which the shell draws). `GET /auth/session` lists the apps; `flows_url` goes. **Delete the Flows mirror**: `frontend/src/flows/FlowsSection.tsx`, `frontend/src/flows/orchestrator.ts`, `frontend/src/flows/FlowsPage.tsx`, the Flows tab of `frontend/src/shell/Panel.tsx`, the `#/flows…` routes of `frontend/src/router.ts`, and the "Flows pages" section of orchestration.md. Queues appear, because neorc draws them. | R | both; New run stays in Robinauts until step 6 |
| 6 | **Forms move to neorc.** Robinauts registers `chat:models`, `chat:agents`, `chat:repo_branch` as option sources. The flows folder's `*.form.toml` become neorc forms. Delete `frontend/src/flows/NewRunPage.tsx`, `intake.ts`, `GET /api/flows` and `POST /api/flows/<name>/runs`, and the forms part of `orchestrator/application/flows.py`. | R, N | both |
| 7 | **The registry, in Robinauts.** Apps found by entry point, mounted at `/apps/<id>/`, with manifests and lifespans. The orchestrator becomes an app package. Roles: the `[[grant]]` table, `shell:admin`, `chat:*`, `neorc:*`; a test that every route declares a permission. | R | both |
| 8 | **Cut the library out.** Create the shell repository with the gates. Move the shell UI, the sign-in, sessions, tokens, grants, the registry and the local mode into it, from Robinauts, with history and an ip-clearance entry. Robinauts pins it by commit. The credentials tables move to the `shell` schema by a data migration. Device pairing stays in Robinauts. | S, R | Robinauts unchanged to its users |
| 9 | **neorc standalone adopts the library.** Its sign-in, sessions and tokens are the library's. `neorc/auth/`, `manager/_sign_in.py`, `manager/_auth_routes.py` and the three credential tables go. Its roles become its declaration; `neorc tokens create` becomes service tokens with a grant. Its standalone UI runs inside the shell page. `--no-auth` stays. | N | neorc standalone, now with the shell |
| 10 | **Publish.** The shell repository and neorc release to PyPI with trusted publishing. Robinauts drops `neorc-from-source` (the steps are in orchestration.md, "Installing neorc") and the git pin of the library. | S, N, R | both, from wheels |

Steps 1 to 7 are all in Robinauts and neorc. They deliver the shell, the
end of the mirror, and flows with sign-in on, with no third repository.
Step 8 is the decision point: by then the code to extract exists, has two
real consumers in view, and can be cut along seams that already hold.

What changes in neorc, in sum: a protocol client and a theme override (2),
an authenticator port, option sources and per-queue grants for people (3),
and later its sign-in replaced by the library's (9). Its core,
`neorc_core`, never depends on the library.

What changes in Robinauts, in sum: the protocol (1), the identity handed to
neorc (4), the shell layout and the deletion of the mirror (5), the forms
moved (6), the registry and roles (7), and the sign-in moved out (8).

## 10. Risks and open questions

### Risks

- **A library cut too early.** Designing the library before steps 1–7
  guesses at needs that working code would show. The order above is the
  mitigation.
- **Three release trains.** A change across the shell and an app is two
  pull requests and a release between them. Pinning by commit softens it
  until step 10.
- **Gates in a third repository.** The licence gate, the audits, the
  bundled-package list, DCO and REUSE must exist from its first commit, or
  Robinauts' supply-chain promise has a hole in it.
- **Same-origin apps are trusted code.** A bug in any app's UI is a bug in
  every app's session. Only installed packages are apps; third-party UI
  stays sandboxed, as MCP views are.
- **The chat in a frame.** One more document, a nested frame for MCP
  views, and the end-to-end suite must reach into the frame. The chat's
  bundle budget is unchanged; the shell page adds its own.
- **Migrating the credentials tables.** Sessions and tokens in use must
  survive step 8, or everyone signs in again and every token is re-minted.
  Decide which, and say so in the release notes.
- **neorc's ownership.** neorc is a separate project. Steps 2, 3, 6 and 9
  are its maintainers' to accept.
- **The overriding stopgap.** Overriding neorc's `guard` through
  `dependency_overrides` would give step 4 without step 3. It relies on an
  internal name. Use it only if step 3 is delayed, and test it.

### Open questions

1. The repository's name, and the packages' names.
2. Chat in a frame of its own (recommended), or drawn by the shell page
   directly. Drawing it directly saves a document and costs the shell a
   dependency on Robinauts' frontend, which fails the discard test.
3. The shell's icon set: the library carries one set (lucide's data, as
   Robinauts does), and an app names an icon from it. Or an app ships an
   SVG, which the shell would have to sanitise.
4. Whether `/api/` stays at the root for Robinauts' existing clients (the
   Android app, scripts with tokens), or moves under `/apps/chat/`. The
   study assumes it stays.
5. Personal tokens across apps: one token that reaches every app the person
   may use, or a token per app. The model allows both; the default is
   undecided.
6. The cluster case: neorc in its own process, connected to Robinauts. The
   signed assertion of section 4.4 is sketched, not specified.
7. Whether neorc standalone must run inside the shell page, or may keep its
   own layout with the library providing only sign-in.
8. Device pairing: in the library, for any app with a phone client, or
   Robinauts' alone. It stays in Robinauts until there is a second phone
   client.

## 11. Recommendation

- **Adopt the proposal's shape**: Robinauts as a shell of apps; one
  same-origin frame per app that draws its three panels; apps plugged in on
  the server by entry point, with a manifest and a lifespan; a small,
  versioned JSON-RPC protocol over `postMessage`, shaped like MCP Apps; the
  shell's sign-in in front of every mount; the identity handed to apps in
  the ASGI scope.
- **Put the shell, sign-in, sessions, tokens, roles and the registry in a
  third repository**, consumed as a library inside each host's process. Not
  a proxy (goal 6), not in neorc (the discard test).
- **But build it in Robinauts first** (steps 1–7), and cut it out at step 8,
  once it works and both consumers are in view. The deletion of the Flows
  mirror, and flows with sign-in on, do not wait for the third repository.
- **Compose where it pays, keep what is written where it does not**: keep
  the OIDC client, the sessions and the plain role tables; borrow MCP Apps'
  envelope; read Luigi before writing the frame host; keep Authlib and
  Casbin as the named fallbacks.
- **Ship the shell UI as a built bundle in the library's wheel**, as
  `neorc-ui` does, so Robinauts' runtime closure stays `react` and
  `react-dom`. Copy the protocol client into each app until a third app
  justifies an npm release.
- **Move the right panel into the Chat app unchanged**, and record in the
  ADR that ADR 0007's "interface" is now the Chat app.

No spike was needed. The one question a spike would have settled, whether a
host can hand neorc an identity without changing neorc, is answered by
reading the code: it can, by overriding `guard`, and it should not, because
`guard` is not an interface (section 2.4).
