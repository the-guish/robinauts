# IP clearance log

An entry is appended whenever something enters the tree other than as a
contributor's own work in an ordinary signed-off pull request: vendored
code, a component copied from a registry, code or text derived from another
project, an asset. The entry is part of the pull request that brings it in.

Each entry states where it came from and at which commit, under what
licence, where it landed, what was changed, and where attribution was
added.

Entries are never edited after the fact; a correction is a new entry.

---

<!-- REUSE-IgnoreStart -->
<!-- The entries quote other projects' copyright and licence lines. They
     are statements about those projects, not about this file. -->

### 2026-09-21 — sign-in design, from neorc

- Source: https://github.com/open-shipyard/neorc, commit `68e3805`,
  `docs/working-notes/sso-plan.md` and the code it describes
  (`neorc_core/_access.py`, `neorc/auth/`, `neorc/manager/`).
- Their licence: Apache-2.0, Copyright The neorc Authors.
- Landed as: `docs/specs/sign-in.md` so far — the design only. The code
  will be written for this project's layout with neorc's code as the
  reference; an entry is added here when it lands.
- Modifications: a users table, person roles, a local development mode; no
  API tokens yet.
- Attribution added to: this entry. No code has been copied, so no notice
  is required yet.

### 2026-09-21 — sign-in rules (domain and core), derived from neorc

- Source: https://github.com/open-shipyard/neorc, commit `68e3805`:
  `python/neorc-core/src/neorc_core/_access.py` (allow-list matching, the
  verified-email rule, secret hashing), `python/neorc/src/neorc/auth/_oidc.py`
  (ID token claim checks, identity extraction, token payload decoding, the
  PKCE challenge), `python/neorc/src/neorc/auth/_config.py` (URL and issuer
  normalisation, the shape of the configuration validator).
- Their licence: Apache-2.0, Copyright The neorc Authors.
- Landed as: `backend/src/robinauts/core/allow.py`, `claims.py`, `urls.py`,
  `sign_in_config.py`, `hashing.py`, and the records they use in
  `backend/src/robinauts/domain/`.
- Modifications: no file was copied. The logic was written again for this
  project's layers — pure functions in `core`, records in `domain`, this
  project's error types and names — with neorc's code open as the
  reference. Substantially machine-generated, reviewed by the maintainers.
- Attribution added to: this entry. Same authors and the same licence; no
  notice is required.

### 2026-09-21 — sign-in flow (ports and application), derived from neorc

- Source: https://github.com/open-shipyard/neorc, commit `68e3805`:
  `python/neorc-core/src/neorc_core/_access.py` (beginning a sign-in, taking
  a pending sign-in, opening a session, sweeping, the cap on pending
  sign-ins), `neorc_core/ports/_credentials.py` (the shape of the store
  port), `neorc_core/local/_memory_credentials.py` and
  `neorc_core/testing/contracts/_credentials.py` (the in-memory store and
  the idea of a contract suite), `python/neorc/src/neorc/auth/_oidc.py`
  (discovery, the authorization URL, the code exchange),
  `python/neorc/src/neorc/manager/_auth_routes.py` (the order of the
  callback's checks).
- Their licence: Apache-2.0, Copyright The neorc Authors.
- Landed as: `backend/src/robinauts/ports/`,
  `backend/src/robinauts/application/sign_in.py`, `backend/tests/fakes/`,
  `backend/tests/contracts/credential_store.py`.
- Modifications: no file was copied. Written again for this project's
  layers; deadlines are computed by the application from a clock port
  rather than by the store; the identity provider port only fetches and
  posts, and the application validates what comes back. Substantially
  machine-generated, reviewed by the maintainers.
- Attribution added to: this entry. Same authors and the same licence; no
  notice is required.

### 2026-09-21 — OIDC HTTP adapter and the stand-in provider, derived from neorc

- Source: https://github.com/open-shipyard/neorc, commit `68e3805`:
  `python/neorc/src/neorc/auth/_oidc.py` (the discovery fetch, the code
  exchange, client authentication by basic or post with the RFC 6749
  section 2.3.1 encoding, timeouts, no redirects) and the `StandInProvider`
  and `unsigned_jwt` of the root `conftest.py`.
- Their licence: Apache-2.0, Copyright The neorc Authors.
- Landed as: `backend/src/robinauts/adapters/identity_provider.py` and
  `backend/tests/standin/`.
- Modifications: no file was copied. The adapter only fetches and posts and
  returns raw data, where neorc's client also validates; the stand-in is a
  real loopback HTTP server, where neorc's is an ASGI application.
  Substantially machine-generated, reviewed by the maintainers.
- Attribution added to: this entry. Same authors and the same licence; no
  notice is required.

### 2026-09-21 — the auth API (routes, cookies, request protection), derived from neorc

- Source: https://github.com/open-shipyard/neorc, commit `68e3805`, under
  `python/neorc/src/neorc/`: `manager/_auth_routes.py` (the four routes,
  the redirect to the sign-in page with a fixed code), `manager/_sign_in.py`
  (the two cookies, the `__Host-` split, the origin check),
  `manager/_access.py` (the guard, the per-route permission declaration),
  `manager/_app.py` (the shape of `create_app`), `_errors.py` (the status
  table and the error body), and `tests/test_http_access.py` (the test that
  every route declares a permission).
- Their licence: Apache-2.0, Copyright The neorc Authors.
- Landed as: `backend/src/robinauts/api/`, `backend/src/robinauts/app.py`.
- Modifications: no file was copied. Request protection is ASGI middleware
  here; a 5xx never says why; start-up problems are merged into one error;
  the collaborators are injectable; the OpenAPI snapshot and the return-to
  landing are this project's. Substantially machine-generated, reviewed by
  the maintainers.
- Attribution added to: this entry. Same authors and the same licence; no
  notice is required.

### 2026-09-22 — frontend tooling and the JavaScript rules, derived from neorc

- Source: https://github.com/open-shipyard/neorc, commit `68e3805`:
  `contributing/js-dependencies.md` (the rules themselves),
  `ts/neorc-ui/vite.config.ts` (the licence allowlist over
  `rollup-plugin-license`, the gzipped size budget, the writer of the
  bundled-package list), `ts/neorc-ui/package.json`, `.npmrc`, `.nvmrc`,
  `eslint.config.js`, `tsconfig*.json`, `index.html`, `src/test/setup.ts`,
  `AGENTS.md`, `README.md`, and the `ui` job of `.github/workflows/ci.yml`.
- Their licence: Apache-2.0, Copyright The neorc Authors.
- Landed as: `frontend/` (`package.json`, `.npmrc`, `.nvmrc`,
  `vite.config.ts`, `eslint.config.js`, `tsconfig.json`,
  `tsconfig.app.json`, `tsconfig.tools.json`, `index.html`,
  `src/test/setup.ts`, `AGENTS.md`, `README.md`),
  `docs/contributing/js-dependencies.md`, `scripts/check-frontend.sh` and
  the `frontend` job of `.github/workflows/ci.yml`. `frontend/.prettierignore`
  and `frontend/scripts/check-licences.mjs` are this project's own: neorc runs
  no formatter and gates only its bundle.
- Modifications: the shape was copied and adapted; none of neorc's
  application code, components, styling or branding was. Every package is
  pinned at the newest version that had been published at least ten days
  before this date, not at neorc's version. The checks run
  from one script, as every other gate in this repository does, rather than
  from steps in the workflow. The bundled-package list is compared by the
  build itself under `CHECK_BUNDLED`, not by `git diff` afterwards. The
  licence allowlist is stated as the allowed list of `DEPENDENCIES.md`, and
  the build tooling that is not on it is recorded by name. The size budget is
  800 KB rather than 500 KB and is marked provisional
  (`docs/specs/frontend.md`, "Open"). The lint rule that confines
  assistant-ui, its test, Prettier, the typed API client and the chat seam
  are this project's and have no counterpart in neorc, as is the npm licence
  gate over everything installed. Substantially machine-generated, reviewed
  by the maintainers.
- Attribution added to: this entry. Same authors and the same licence; no
  notice is required.

### 2026-09-22 — the interface's design tokens and the shape of its shell, derived from neorc

- Source: https://github.com/open-shipyard/neorc, commit `68e3805`, under
  `ts/neorc-ui/src/`: `index.css` (the design tokens — the ground, paper,
  panel, tint, hover, ink, muted, line, accent, link and warning colours in
  light and dark, and the three radii — and the rules for the sidebar, the
  rail, the profile block, the banner and the sign-in card),
  `components/Layout.tsx` (the shape of the shell: the collapse button and
  the brand, the primary action, the sections, the profile block pinned to
  the bottom, the rail state in `localStorage`), `components/SignIn.tsx`
  (the sign-in card and the sentence per error code), `session.ts` (asking
  once who is signed in, signing out, a 401 ending the session) and
  `App.tsx` (the session gate in front of every page).
- Their licence: Apache-2.0, Copyright The neorc Authors.
- Landed as: `frontend/src/tokens.css`, `frontend/src/styles.css`,
  `frontend/src/shell/`, `frontend/src/session/`, `frontend/src/App.tsx`.
- Modifications: no file was copied. **The token values are neorc's**, name
  for name and colour for colour, because they are the visual identity this
  project was asked to carry over; the status, graph and table colours were
  left behind, `--scrim` was added for the drawer, and the whole set is
  written once and mapped twice rather than repeated under the media query,
  with a `[data-theme]` override for the toggle that neorc has no
  counterpart for. The CSS rules themselves were **not** carried over: the
  interface is written in Tailwind utilities over the tokens (ADR 0001),
  where neorc writes hand-rolled classes. The components were written again
  for this project: React 19 with `useSyncExternalStore` and no
  data-fetching library where neorc uses TanStack Query, our typed API
  client, an `onUnauthorized` callback rather than a query invalidation,
  `return_to` as a query parameter of the login route rather than
  `sessionStorage`, the small-screen drawer and the light/dark/system
  toggle, which neorc has neither of, and `lucide-react` rather than
  hand-drawn SVG icons. The error sentences say "deployment" where neorc's
  say "manager" and are otherwise the same set of eight, because the codes
  are the same eight. Substantially machine-generated, reviewed by the
  maintainers.
- Attribution added to: this entry. Same authors and the same licence; no
  notice is required.

### 2026-09-22 — the assistant-ui styled chat components, vendored

- Source: two shadcn-style registries, fetched with `curl` on 2026-09-22
  (01:14 UTC on 2026-09-23). Neither registry states a version or a commit of
  its own — each is served from its project's default branch — so the commit
  recorded is that branch's head at the moment of the fetch, read with
  `git ls-remote`.
- From `https://r.assistant-ui.com/<item>.json`, out of
  https://github.com/assistant-ui/assistant-ui at commit
  `92d16d77a684cff61e6812c0803ea698a8db8b2f`: the items `thread`,
  `markdown-text`, `tooltip-icon-button`, `use-copy-to-clipboard`,
  `follow-up-suggestions`, `file`, `image`, `reasoning`, `elements-reasoning`,
  `tool-fallback` and `tool-group` — one file each, eleven in all, which is
  what `thread` needs less the attachment items. The `utils` item was read and
  **not** copied: it now re-exports a `cn` package this project does not take,
  so `frontend/src/chat/assistant-ui/vendor/lib/utils.ts` is written here
  instead and is this project's own work under Apache-2.0.
- From `https://ui.shadcn.com/r/styles/new-york-v4/<item>.json`, out of
  https://github.com/shadcn-ui/ui at commit
  `98a1fe67b439324ddc857f47fbdce056600a4329`: `button`, `skeleton`, `tooltip`,
  `textarea` and `collapsible`, the five components the assistant-ui items
  import.
- Their licences: MIT, Copyright (c) 2025 AgentbaseAI Inc. (assistant-ui);
  MIT, Copyright (c) 2023 shadcn (shadcn/ui). Both texts were fetched from
  their repositories at the commits above.
- Landed as: sixteen files under `frontend/src/chat/assistant-ui/vendor/`, at
  the paths the registry items name, beside `vendor/lib/utils.ts`, which is
  ours. Upstream's licence texts sit beside them as `vendor/LICENSE` and
  `vendor/LICENSE.shadcn-ui`; `vendor/README.md` is this project's and holds
  the file list, the modifications, the per-package vetting judgements and the
  re-sync procedure; `frontend/src/test/vendor.test.ts` holds the list and the
  directory to each other.
- Modifications, six, each with its reason in `vendor/README.md`: the `@/`
  path aliases rewritten to relative paths (this package has no `paths`, and
  adding them would give every path a second spelling the seam rules of
  ADR 0001 refuse); `lib/utils.ts` written here as the `clsx`/`tailwind-merge`
  implementation of `cn` rather than a re-export of the three-week-old `cn`
  package, and the five shadcn components pointed at it; the attachment
  composer removed from `thread.aui.tsx` (the POC has no attachments); one
  field read as optional in `tool-fallback.aui.tsx`, because the registry's
  copy is written against a newer `@assistant-ui/react` than the ten-day
  cooldown allows pinning; formatting with this repository's Prettier; and two
  ESLint rules turned off for that directory alone (`no-empty` relaxed to
  allow an empty `catch`, `react-hooks/refs` off), in
  `frontend/eslint.config.js` with the reasons written there. Nothing else was
  touched. The CLI (`npx shadcn@latest add`) was **not** run: it installs
  packages, runs install scripts and rewrites configuration, none of which
  this repository lets a tool do.
- Attribution added to: this entry, `docs/legal/third-party.md`, the two
  licence texts beside the code, `LICENSES/MIT.txt` and the annotations for
  the directory in `REUSE.toml`. The copy was made by The Robinauts Authors
  via Claude Code, reviewed by the maintainers.

### 2026-10-02, API tokens: a correction to the sign-in design entry

- Corrects: "sign-in design, from neorc" (2026-09-21), whose modifications
  end "no API tokens yet". API tokens have landed.
- Source: this project. The spec had them planned with no design; block 7
  designed them (`docs/working-notes/oct-refactor/auth-plan.md`, "API
  tokens") and step 6 of that plan wrote them.
- Their licence: Apache-2.0, this project's.
- Landed as: `docs/specs/sign-in.md`, "API tokens", and the three
  `/auth/tokens` routes in `backend/src/robinauts/web/app.py`.
- Modifications: the 2026-09-21 entry's list gains API tokens. A signed-in
  person mints one for themselves; it is shown once, kept as its SHA-256,
  lives ninety days and is sent as a bearer.
- Attribution added to: none needed. Nothing was taken from another project.

<!-- Entries go above this line. -->
<!-- REUSE-IgnoreEnd -->
