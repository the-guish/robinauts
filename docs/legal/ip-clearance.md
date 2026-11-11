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

### 2026-10-07, moved files: a correction to the sign-in entries

- Corrects: the "Landed as" paths of four entries of 2026-09-21. The
  controller refactor moved the sign-in code, and no content changed origin.
  - "sign-in rules (domain and core)": `core/allow.py`, `claims.py`,
    `urls.py`, `sign_in_config.py`, `hashing.py` and `domain/` are now
    `backend/src/robinauts/web/sign_in.py`, `backend/src/robinauts/web/oidc.py`
    and `backend/src/robinauts/controller/contract/domain.py`.
  - "sign-in flow (ports and application)": `ports/`,
    `application/sign_in.py`, `tests/fakes/` and
    `tests/contracts/credential_store.py` are now
    `backend/src/robinauts/controller/contract/ports.py`,
    `backend/src/robinauts/web/sign_in.py`,
    `backend/src/robinauts/controller/adapters/memory/credentials.py`,
    `backend/src/robinauts/controller/adapters/postgres/credentials.py` and
    `backend/tests/util/contracts/credentials.py`.
  - "OIDC HTTP adapter and the stand-in provider":
    `adapters/identity_provider.py` and `tests/standin/` are now
    `backend/src/robinauts/web/oidc.py` and `backend/tests/util/standin/`.
  - "the auth API": `api/` and `app.py` are now
    `backend/src/robinauts/web/app.py` and `backend/src/robinauts/web/cookies.py`.
- Also corrects: the 2026-10-02 entry cites
  `docs/working-notes/oct-refactor/auth-plan.md`. This project's working
  notes left the tree in commit `2da7ead` (2026-10-03). The note is in the
  history before that commit.
- Source: this project.
- Their licence: Apache-2.0, this project's.
- Landed as: this entry.
- Modifications: none to any code. Only the record of where it lives changes.
- Attribution added to: none needed.

### 2026-11-04, lucide's icon path data, copied

- Source: the npm package `lucide-react` at version 1.61.0, as installed by
  `npm ci` from the lockfile of the day, out of
  https://github.com/lucide-icons/lucide. For each icon, the file
  `dist/esm/icons/<name>.mjs` of the installed package was read, and the
  icon's node was taken from it: the list of SVG elements and their
  attributes that the file hands to lucide's icon factory.
- What was copied: the path data of 22 icons, and nothing else of the
  package: `arrow-down`, `arrow-up`, `brain`, `check`, `chevron-down`,
  `circle-x`, `copy`, `ellipsis`, `loader`, `log-out`, `menu`, `monitor`,
  `moon`, `panel-left`, `pencil`, `play`, `plus`, `refresh-cw`, `square`,
  `sun`, `trash`, `x`. These are the icons the interface drew from the
  package before it was removed
  ([ADR 0006](../adr/0006-chat-ui-on-react-alone.md)). The copy is these 22
  icons: `circle-alert`, copied at first, was drawn nowhere and was taken
  out of the file again, regenerated from the same source, before this
  branch landed.
- Their licence: ISC, Copyright (c) 2026 Lucide Icons and Contributors. The
  package's `LICENSE` also states that the icons it lists are derived from
  the Feather project under MIT, Copyright (c) 2013-present Cole Bemis.
  `arrow-down`, `arrow-up`, `check`, `chevron-down`, `loader`, `log-out`,
  `monitor`, `moon`, `plus`, `square`, `trash` and `x` are in that list by
  name, and `circle-x`, `ellipsis` and `panel-left` are in it under the
  names lucide gave them before (`x-circle`, `more-horizontal`,
  `sidebar`). The file is therefore annotated
  `ISC AND MIT`. The text was copied whole from the same package.
- Landed as: `frontend/src/icons/lucide.ts` (the data) and
  `frontend/src/icons/LICENSE.lucide` (the package's `LICENSE`, unchanged).
  `frontend/src/icons/index.tsx`, the components over the data, and
  `frontend/src/icons/README.md` are this project's own code and carry its
  header.
- Modifications: the React `key` attribute of each element was dropped,
  because the components here render the list with their own keys. Nothing
  else was modified: every element name, attribute and value is as the
  package ships it. The icons are keyed by lucide's own names. The file's
  opening comment, which states where it came from, is ours.
- Attribution added to: this entry, `docs/legal/third-party.md`, the licence
  text beside the data, `LICENSES/ISC.txt` and the annotation for the two
  files in `REUSE.toml`. The copy was made by The Robinauts Authors via
  Claude Code, reviewed by the maintainers.

### 2026-11-04, the vendored assistant-ui and shadcn/ui components, removed

- Removes: what the entry "the assistant-ui styled chat components,
  vendored" (2026-09-22) brought in. That entry stays as it was, as the
  record of what was in the tree and when.
- Source: this project. [ADR 0006](../adr/0006-chat-ui-on-react-alone.md)
  replaces the chat window with this project's own code.
- Their licences: MIT, Copyright (c) 2025 AgentbaseAI Inc. (assistant-ui);
  MIT, Copyright (c) 2023 shadcn (shadcn/ui).
- Removed: the whole of `frontend/src/chat/assistant-ui/vendor/`, which is
  the sixteen copied files under `components/` and `hooks/`, the two licence
  texts beside them (`vendor/LICENSE` and `vendor/LICENSE.shadcn-ui`), and
  this project's own `vendor/README.md` and `vendor/lib/utils.ts`.
  `frontend/src/test/vendor.test.ts`, which held the directory to its list,
  went with it, as did the two ESLint relaxations scoped to the directory.
- Modifications: none. Nothing of either project's code, markup, class names
  or text remains in the tree. The chat window that replaces it was written
  for this project, and the end-to-end suite holds it to the same roles and
  accessible names as before, which are this project's interface and not
  upstream's code.
- Attribution removed from: `docs/legal/third-party.md`, which no longer
  lists the components, and `REUSE.toml`, which no longer annotates the
  directory. `LICENSES/MIT.txt` stays, because the icon data of the entry
  above is in part MIT.

### 2026-11-08, lucide's `panel-right` icon, copied

- Source: the npm package `lucide-react` at version 1.61.0, the version of
  the entry "lucide's icon path data, copied" (2026-11-04), out of
  https://github.com/lucide-icons/lucide. The package was fetched with
  `npm pack lucide-react@1.61.0` into a scratch directory outside the tree
  and unpacked there; nothing was installed. The icon's node was read from
  `dist/esm/icons/panel-right.mjs`.
- What was copied: the path data of one icon, `panel-right`: a rounded
  rectangle and one line. It is the toggle of the right side panel, the
  mirror of `panel-left` on the left one.
- Its licence: ISC, Copyright (c) 2026 Lucide Icons and Contributors. It is
  not in the package `LICENSE`'s list of icons derived from Feather, so it is
  ISC alone. The `LICENSE` of that tarball is identical to
  `frontend/src/icons/LICENSE.lucide`, which was checked with `diff`.
- Landed as: one entry appended at the end of `frontend/src/icons/lucide.ts`,
  and a `PanelRightIcon` component in `frontend/src/icons/index.tsx`, which
  is this project's code.
- Modifications: the React `key` attribute of each element was dropped, as
  for the other icons. Nothing else.
- Attribution added to: this entry, and the count of icons in
  `docs/legal/third-party.md` and `frontend/src/icons/README.md`. The
  annotation in `REUSE.toml` already covers the file, as `ISC AND MIT`. The
  copy was made by The Robinauts Authors via Claude Code, reviewed by the
  maintainers.

### 2026-11-09, lucide's `workflow` icon, copied

- Source: the npm package `lucide-react` at version 1.61.0, the version of
  the entry "lucide's icon path data, copied" (2026-11-04), out of
  https://github.com/lucide-icons/lucide. The package was fetched with
  `npm pack lucide-react@1.61.0` into a scratch directory outside the tree
  and unpacked there; nothing was installed. The icon's node was read from
  `dist/esm/icons/workflow.mjs`.
- What was copied: the path data of one icon, `workflow`: two rounded squares
  and the path joining them. It marks "Flows", the left panel's link to the
  orchestrator's page.
- Its licence: ISC, Copyright (c) 2026 Lucide Icons and Contributors. It is
  not in the package `LICENSE`'s list of icons derived from Feather, so it is
  ISC alone. The `LICENSE` of that tarball is identical to
  `frontend/src/icons/LICENSE.lucide`, which was checked with `diff`.
- Landed as: one entry appended at the end of `frontend/src/icons/lucide.ts`,
  and a `WorkflowIcon` component in `frontend/src/icons/index.tsx`, which
  is this project's code.
- Modifications: the React `key` attribute of each element was dropped, as
  for the other icons. Nothing else.
- Attribution added to: this entry, and the count of icons in
  `docs/legal/third-party.md` and `frontend/src/icons/README.md`. The
  annotation in `REUSE.toml` already covers the file, as `ISC AND MIT`. The
  copy was made by The Robinauts Authors via Claude Code, reviewed by the
  maintainers.

### 2026-11-11, lucide's `archive` icon, copied

- Source: the npm package `lucide-react` at version 1.61.0, the version of
  the entry "lucide's icon path data, copied" (2026-11-04), out of
  https://github.com/lucide-icons/lucide. The package was fetched with
  `npm pack lucide-react@1.61.0` into a scratch directory outside the tree
  and unpacked there; nothing was installed. The icon's node was read from
  `dist/esm/icons/archive.mjs`.
- What was copied: the path data of one icon, `archive`: a lid, a box under
  it and a handle. It marks "Archive" on a conversation in the panel.
- Its licence: ISC, Copyright (c) 2026 Lucide Icons and Contributors. It is
  not in the package `LICENSE`'s list of icons derived from Feather, so it is
  ISC alone. The `LICENSE` of that tarball is identical to
  `frontend/src/icons/LICENSE.lucide`, which was checked with `diff`.
- Landed as: one entry appended at the end of `frontend/src/icons/lucide.ts`,
  and an `ArchiveIcon` component in `frontend/src/icons/index.tsx`, which
  is this project's code.
- Modifications: the React `key` attribute of each element was dropped, as
  for the other icons. Nothing else.
- Attribution added to: this entry, and the count of icons in
  `docs/legal/third-party.md` and `frontend/src/icons/README.md`. The
  annotation in `REUSE.toml` already covers the file, as `ISC AND MIT`. The
  copy was made by The Robinauts Authors via Claude Code, reviewed by the
  maintainers.

<!-- Entries go above this line. -->
<!-- REUSE-IgnoreEnd -->
