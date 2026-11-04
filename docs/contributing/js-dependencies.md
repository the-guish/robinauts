# JavaScript dependencies

Rules for the packages under `frontend/`. Their code ships to users inside the
`robinauts` wheel, compiled into a bundle nobody reads, and runs in a browser
beside a signed-in person's session, able to do whatever they may. Every
dependency is code the project vouches for, at every version it takes.

The policy behind the licence rules is [../../DEPENDENCIES.md](../../DEPENDENCIES.md);
this file says how it is applied to npm.

## Keep them few

- Add a runtime dependency only when writing the code would cost more than
  vetting the package, and its transitive tree, now and at every update.
- `dependencies` is what the bundle ships; `devDependencies` is build tooling.
  Keep the first list as short as it can be.
- `frontend/bundled-packages.txt` lists every package that ends up in the
  bundle. The build writes it and CI fails when it differs, so a new bundled
  package is a line in the pull request diff. Never edit it by hand. It is
  written from **rollup's module graph**, so it names every package a *module*
  was imported from — and only those.
- **A package reached only through a stylesheet is in no module graph.**
  `@import "some-package"`, `url(some-package/logo.png)`, and the `@import`s
  inside a package's own stylesheet are all outside rollup's, so nothing
  discovers them. **No package is reached that way today**: the stylesheets
  are plain CSS over the design tokens, and none of them names a package
  ([../adr/0006-chat-ui-on-react-alone.md](../adr/0006-chat-ui-on-react-alone.md)).
  The mechanism stays for the day one is. Such a package is written down by
  hand, in `CSS_PACKAGES` in `frontend/vite.config.ts`, now empty: a
  stylesheet that gains one of those adds the package's name there, and that
  line is what a reviewer looks at — say so in the pull request as well. From
  the name, the build holds it to the allowed list, adds it to
  `bundled-packages.txt` beside the module graph's packages, and appends its
  `LICENSE` to `dist/THIRD_PARTY_LICENSES.txt`, so nothing ships unrecorded
  or unattributed. What is still lost is a name nobody wrote down;
  [../../DEPENDENCIES.md](../../DEPENDENCIES.md) says the whole of it.
- **Plain CSS, no CSS Modules, no inline `<style>`.** Three things the build
  refuses outright, because each can be judged from a file's name without
  resolving anything: a CSS Module (`composes ... from` and `@value ... from`
  are resolved by postcss-modules, where rollup never sees a module at all —
  that holds for a dependency's stylesheets too, so pick one that ships plain
  CSS), a stylesheet in any language but `.css`, and a `<style>` block in
  `index.html`, which would need `unsafe-inline` in the
  Content-Security-Policy anyway.

## Vet before adding

- Maintained: recent releases, issues answered, and **more than one
  maintainer — or a single maintainer with years of settled releases and wide
  adoption**. The second half is not a loophole; it is the truth about a
  handful of packages everything depends on. What makes it
  a rule rather than a shrug is that the judgement is **written down** where
  the package is adopted: which of the two it is, and why.
- Small: `npm view <pkg> dependencies` is short, and the size fits the budget.
- Licensed on the list below.
- Published with provenance, `npm view <pkg> dist.attestations` — **required
  of a release published after npm had provenance, which is April 2023**. An
  older release cannot carry one and is not refused for that; it is accepted
  when the package has years of settled history behind it, and, again, the
  judgement is written down. A package whose first release is later than that
  date and which publishes without attestations is refused.
- Needs no install script, `npm view <pkg> hasInstallScript`. A package that
  does is a reason to pick another.
- Read what npm serves, `npm pack <pkg>` and look inside; the GitHub
  repository is not necessarily what was published.

## Licences

A bundled package carries a licence on the **allowed list of
[../../DEPENDENCIES.md](../../DEPENDENCIES.md)**, and that document is where
the list is — `frontend/scripts/check-licences.mjs` reads it out of the two
Categories paragraphs, and `frontend/vite.config.ts` imports it from there, so
the list is written once and restated nowhere. The build fails on anything
else, and on a licence it cannot recognise. The wheel is Apache-2.0; a
copyleft library in the bundle would put that in question. Extending the list
is a reviewed change to `DEPENDENCIES.md`, with a stated reason.

### Build tooling

Build tooling is not distributed: it is not in the bundle, not in the wheel,
not linked into anything, and it is not what the build-time gate looks at —
that gate reads what rollup put in the bundle, which is the code that ships.
It is still held to the same policy, because `DEPENDENCIES.md` asks the same
questions of a development dependency as of any other. What is not plainly on
the allowed list is excepted by name and version in the **"JavaScript build
tooling"** table of [../../DEPENDENCIES.md](../../DEPENDENCIES.md) — the one
place an npm exception may be written; there is no exception list in this
file. `frontend/scripts/check-licences.mjs` enforces it over everything the
lockfile pins and npm installed: no row, a row naming another version, a row
naming another licence, a row for a package that no longer needs one, and the
build is red. A forbidden licence fails whatever a row says, and the list of
what is forbidden is shared with the Python gate through
`scripts/licence-fixtures.json`, which both test suites read.

Each row also states a **scope**, `development` or `runtime`, which is
checked against the lockfile rather than believed: npm marks a package `dev`
only when every path to it is a development dependency. A restricted licence
— the MPL-2.0 family — may only be carried by a `development` row. The scope
is about dependency edges, not about the module graph, so it says nothing
about what reached the bundle. It is also the one place the bundle's record
can be wrong about something that matters: see the bullet above, and
`DEPENDENCIES.md`.

That script also refuses a version in `package.json` that is not exact, in
`dependencies`, `devDependencies`, `optionalDependencies`, `peerDependencies`
and `overrides` alike.

So a bump that changes a licence stops the build until somebody reads the
licence again and writes down what they read.

## Installing and updating

- Exact versions in `package.json`, no ranges. `package-lock.json` is
  committed and installs are `npm ci`, never `npm install`.
- `frontend/.npmrc` sets `ignore-scripts=true`; install scripts do not run on
  any machine or in CI.
- Updates come from Dependabot, with a cooldown: no version published fewer
  than 10 days ago. Do not update by hand to a version younger than that;
  compromised releases are usually withdrawn within days.
- **The cooldown holds when a package is first adopted, not only when it is
  bumped.** `latest` is often a release of that morning, and a first pin is
  exactly as good a place to plant a compromised version as an upgrade is.
  Pin the newest version published at least ten days ago:
  `npm view <pkg> time --json` lists every version with the day it appeared.
  No script can check this after the fact — the dates are not in the lockfile
  — so it is checked when the pin is written, and the dates that were read go
  in the pull request.
- One dependency change per pull request, never as a side effect of a feature.
- CI runs `npm audit --audit-level=low` for known advisories on everything
  installed, and `npm audit signatures --omit=dev` for registry signatures and
  provenance on what ships: the registry does not hold attestations for every
  dev-only package.
- Node.js at the version in `frontend/.nvmrc`.

## In the browser

- The backend serves `/ui/` with a Content-Security-Policy that allows scripts
  and connections from its own origin only, and styles from it or inline,
  which React needs for `style` attributes
  ([../specs/frontend.md](../specs/frontend.md)). Do not loosen it for a
  library; choose another library.
- No CDN, no external font and no request outside our own API. Every asset is
  in the wheel, so the UI works on air-gapped hosts.

## Releases

Bundles are built in CI, from the committed lockfile and `.nvmrc`. Built
assets are never committed, and a bundle built on a laptop is never released.

## Where this is enforced

- `frontend/vite.config.ts`: the licence allowlist over the bundle, the size
  budget, the bundled-package list, and the three stylesheet refusals.
- `frontend/scripts/check-licences.mjs`: the same policy over everything
  installed, the exceptions table of `DEPENDENCIES.md`, the scopes, and the
  exact pins. Its own refusals are tested in
  `frontend/src/test/licence-gate.test.ts`.
- `frontend/.npmrc`: exact versions and no install scripts.
- `frontend/eslint.config.js`: the seam in front of the chat
  ([../adr/0006-chat-ui-on-react-alone.md](../adr/0006-chat-ui-on-react-alone.md)),
  which lets nothing outside `frontend/src/chat/` import anything under it but
  `src/chat/index.ts` —
  `import`, `export ... from`, `import()` with a string or a template, and a
  refusal of any specifier that cannot be read at compile time, of
  `import.meta.glob` and of `require` — which
  `frontend/src/test/seam-rule.test.ts` proves still fire.
- `scripts/licence-fixtures.json`: what the policy forbids, read by both
  gates' test suites so that the Python and npm copies cannot drift.
- `scripts/check-frontend.sh`, and the `frontend` job of
  `.github/workflows/ci.yml` that runs it: the audits, the build and the
  committed list. A red step there names this file.
- `.github/dependabot.yml`: the updates and their cooldown.
- `frontend/AGENTS.md`: the same rules, as do-nots for coding agents.
