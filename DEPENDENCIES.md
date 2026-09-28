# Dependency policy

Robinauts is Apache-2.0, and a dependency must not put that in question —
for a company running it, or for a product embedding it. The policy is the
Apache Software Foundation's category model, which is the strictest in
common use. The reasoning is in
[docs/specs/open-source.md](docs/specs/open-source.md).

The policy applies to every dependency **and to everything it brings with
it**: the whole locked set is checked, not the direct dependencies alone.

## Categories

### Allowed

Apache-2.0, MIT, BSD-2-Clause, BSD-3-Clause, 0BSD, ISC, Zlib, PostgreSQL,
PSF-2.0, CC0-1.0, Unlicense.

### Restricted

MPL-2.0, EPL-2.0, CDDL-1.x.

Only as an **unmodified, unbundled** dependency, and only if it is listed
by name below with its reason. Never in the JavaScript bundle, which is
bundled code: the bundle uses the allowed list only.

### Forbidden

GPL (any version), AGPL, LGPL (any version, dynamic linking included),
SSPL, BSL, Elastic-2.0, Commons Clause, CC-BY-NC, CC-BY-ND, any "no
commercial use" or "do no evil" term, proprietary licences, and **no licence
at all**.

Forbidden means forbidden transitively, and for development-only and
test-only dependencies too.

## Development-only and test-only dependencies

They follow the same categories. One may be excepted only by name, below,
with the licence somebody read, the version they read it in, and the reason.

There are three tables of exceptions below, and every one of them is in this
file — a dependency is excepted here or nowhere. Two are Python's,
["Restricted dependencies in use"](#restricted-dependencies-in-use) and
["Excepted development-only dependencies"](#excepted-development-only-dependencies),
and `scripts/licence_gate.py` reads them against `backend/uv.lock`. The third
is npm's, ["JavaScript build tooling"](#javascript-build-tooling), and
`frontend/scripts/check-licences.mjs` reads it against
`frontend/package-lock.json` and what npm installed. They are separate tables
because the two gates read two different lockfiles, not because the rules
differ; the rules are the ones on this page.

What ships in the JavaScript bundle is not excepted anywhere: it is the
allowed list or nothing, enforced at build time by `rollup-plugin-license`.
What that gate can and cannot see is written out under
["JavaScript build tooling"](#javascript-build-tooling) below.

An exception is a signature, not a switch. It can cover metadata that names
a licence family and no more — the trove classifier
`License :: OSI Approved :: BSD License` names no version of the BSD licence,
and only a person can open the package and see which one it is. It can never
cover a forbidden licence, a package that states no licence at all, or
metadata nobody can read; and it holds for the version it names, so the next
upgrade fails until somebody reads the licence again.

## Assets

Fonts, icons, images, fixtures, sample data, model files and tokenizers
need a stated licence on the allowed list, and an entry in
[docs/legal/third-party.md](docs/legal/third-party.md). No model weights or
datasets are shipped. No third-party logos are shipped.

## Adopting or upgrading a dependency

1. One dependency change per pull request, and the pull request says why
   the dependency is needed and what was considered instead. A framework and
   the provider clients it is reached through count as **one** change: they
   are adopted or rejected together — a framework with no client reaches no
   vendor — and the gate reads the whole tree either way, so splitting them
   over two pull requests would mean reviewing half a tree twice.
2. The licence of the package and of everything it brings is on the allowed
   list — or restricted, and then added to the table below in the same
   pull request. The licence is re-checked on every upgrade: a minor
   version has changed a licence before.
3. The package is what it claims to be: look at what the registry actually
   serves, not only at the repository.
4. The lockfile is committed.

Points 2 and 4 are enforced by CI, with a licence gate over the whole
locked set: a dependency that fails is a broken build, not a ticket. Run it
yourself with `scripts/check-licences.sh`, from the root of the repository.

The gate reads the categories above and the tables below out of this
document, so a dependency is excepted here or nowhere. It fails closed: a
licence it cannot resolve to an identifier stops the build as surely as a
forbidden one. Metadata that names only a family resolves to nothing — the
classifier `License :: OSI Approved :: BSD License` names no version of the
BSD licence, and the gate will not guess one. It reads that metadata from the
environment `uv` synced from this lockfile, or — for a package locked from
PyPI and not installed here — from the very file this lockfile pins by hash,
whose metadata PyPI publishes with a digest of its own. Metadata it cannot
tie to the lock, or cannot verify, it does not use, and a package it cannot
read is a package that does not pass.

A package may state its licence three times over — in `License-Expression`,
in the free-text `License` field and in its classifiers — and the three do
not always agree. Every claim that names a licence counts, and the worst of
them decides: a package claiming MIT in one field and the GPL in another is a
question for a person, not something to settle in our own favour. A claim
that names no licence counts for nothing, because that field so often holds
the licence text rather than its name.

## Tools that are not dependencies

`reuse` and `pip-audit` run as isolated tools (`uvx`), in their own throwaway
environments, and are deliberately **not** in `backend/uv.lock`: `reuse`
brings `python-debian`, which is GPL-3.0, and adding it would put a forbidden
licence in our locked set for the sake of a program that only reads our files.
Neither is imported, linked or shipped; each is a separate process that looks
at the tree and exits.

Both are pinned to an exact version in
[scripts/tool-versions.sh](scripts/tool-versions.sh) and bumped by hand —
Dependabot does not see them. The licence gate itself needs no tool at all:
it is the Python standard library.

## Restricted dependencies in use

Three things in each row below are checked against reality on every run, so a
dependency that changes under us breaks the build rather than slipping
through on its name:

- the **licence** must be the one the package's own metadata states, and the
  only restricted licence it states. A package offered under a choice of two
  restricted licences fails until the row says which one we rely on;
- the **scope**, where it says *development only*: the gate fails if the
  runtime dependencies ever bring the package in;
- the row must name a licence that is restricted at all — a row is not a way
  to invent a category.

This table is the **Python** locked set's, and a test asserts that every row
of it names a package `backend/uv.lock` really has, so an npm package cannot
go in it. npm's restricted case, `lightningcss`, is a row of
["JavaScript build tooling"](#javascript-build-tooling) below, which is the
table the npm gate reads; the conditions it has to meet are the ones stated
here, and that gate checks them.

| package | licence | scope | why it is acceptable |
|---|---|---|---|
| `pathspec` | MPL-2.0 | development only; brought by `black` | unmodified, not shipped in any artifact |
| `certifi` | MPL-2.0 | runtime; brought by `httpx` and `httpcore` | unmodified, and not bundled: it is installed by the package manager from PyPI, never vendored into this repository and never copied inside the wheel. It is the CA bundle the HTTPS client verifies identity providers with, which is why the sign-in adapter can insist on TLS verification with no way to turn it off |
| `orjson` | MPL-2.0 | runtime; brought by `langgraph-sdk` and `langsmith`, which `langgraph` and `langchain-core` require | unmodified, unbundled, installed from PyPI as a wheel and never vendored. Its metadata states `MPL-2.0 AND (Apache-2.0 OR MIT)`: its own code is dual Apache-2.0/MIT and the MPL-2.0 part is code it carries; MPL-2.0 is the only restricted licence it names. Both packages that bring it are hard dependencies of the framework. `langgraph-sdk` is the LangGraph Platform client and is not used at all — the adapter compiles its graph with no checkpointer and reaches no platform. `langsmith` is used for exactly one thing: `langsmith.configure(enabled=False)`, which is how the adapter turns hosted tracing off ([docs/specs/agents.md](docs/specs/agents.md)); nothing is sent to it, and no LangSmith client is ever built |

## Excepted development-only dependencies

The licence is the one that was read, and the version is the one it was read
in; the gate checks both.

| package | version | licence | why it is acceptable |
|---|---|---|---|
| `colorama` | 0.4.6 | BSD-3-Clause | development only; brought by `pytest` on Windows. Its metadata states the classifier `License :: OSI Approved :: BSD License` and nothing else, which names no version of the BSD licence; the LICENSE.txt shipped in the 0.4.6 wheel is the three-clause text |

## JavaScript build tooling

The packages under `frontend/` that are **not** in the bundle: Vite, ESLint,
Vitest and what they bring. They are not distributed — not in the bundle, not
in the wheel, not linked into anything — but `DEPENDENCIES.md` asks the same
questions of a development dependency as of any other, so the ones whose
licence is not plainly on the allowed list are excepted here by name, with the
version the licence was read in.

The gate is `frontend/scripts/check-licences.mjs`, which reads this table and
the licence of every package `frontend/package-lock.json` locks and npm
installed. A package outside the allowed list with no row here, or with a row
naming a different version or a different licence, fails the build; so does a
row for a package that is installed and no longer needs one. A forbidden
licence fails whatever a row says.

The **scope** column is `development` or `runtime`, and it is checked against
the lockfile rather than believed: npm marks a package `dev` only when every
path to it is a development dependency, so a row saying `development` for a
package the runtime dependencies reach is a failure. A **restricted** licence
— the MPL-2.0 family — may only ever be carried by a `development` row, which
is the restricted category's "unmodified, unbundled" condition made into a
check. Every row below is `development`: none of this is shipped code.

Two things need no row. A licence that is a **choice** including an allowed
one is allowed — `type-fest` states `MIT OR CC0-1.0`, and MIT is on the list.
A licence that is a **conjunction** is allowed only if every part of it is, so
`(MIT AND CC-BY-3.0)` is here.

What ships in the bundle is excepted nowhere: it is the allowed list or
nothing, enforced at build time by `rollup-plugin-license` in
`frontend/vite.config.ts` and recorded in `frontend/bundled-packages.txt`. A
`development` scope below is a statement about dependency edges, not about
the bundle, and excuses nothing there.

| package | version | scope | licence | why it is acceptable |
|---|---|---|---|---|
| `lightningcss`, `lightningcss-android-arm64`, `lightningcss-darwin-arm64`, `lightningcss-darwin-x64`, `lightningcss-freebsd-x64`, `lightningcss-linux-arm-gnueabihf`, `lightningcss-linux-arm64-gnu`, `lightningcss-linux-arm64-musl`, `lightningcss-linux-x64-gnu`, `lightningcss-linux-x64-musl`, `lightningcss-win32-arm64-msvc`, `lightningcss-win32-x64-msvc` | 1.33.0 | development | MPL-2.0 | **restricted**, and the restricted category's conditions hold as they do for `pathspec`: unmodified, unbundled, installed by npm from the registry and never vendored, development only, and in no artefact this project ships. The CSS tool Vite compiles stylesheets with; one package plus the prebuilt binary for each platform, all of which the lock pins, and of which a machine installs the one it can run |
| `lightningcss`, `lightningcss-android-arm64`, `lightningcss-darwin-arm64`, `lightningcss-darwin-x64`, `lightningcss-freebsd-x64`, `lightningcss-linux-arm-gnueabihf`, `lightningcss-linux-arm64-gnu`, `lightningcss-linux-arm64-musl`, `lightningcss-linux-x64-gnu`, `lightningcss-linux-x64-musl`, `lightningcss-win32-arm64-msvc`, `lightningcss-win32-x64-msvc` | 1.32.0 | development | MPL-2.0 | the same packages, the same licence — the MPL-2.0 text ships in the 1.32.0 package too — and the same conditions as the row above. A second copy, because `@tailwindcss/node` depends on `lightningcss` at exactly `1.32.0` while Vite is on 1.33.0, so the lock holds both and the gate asks for the version somebody read |
| `caniuse-lite` | 1.0.30001810 | development | CC-BY-4.0 | a data set of browser support, which the build reads to decide what to compile down to. Attribution only, no copyleft term, and none of it reaches the bundle. Brought by `vite` through `browserslist` |
| `spdx-exceptions` | 2.5.0 | development | CC-BY-3.0 | the SPDX list of licence exceptions — the data `rollup-plugin-license` judges everything else by. Attribution only |
| `spdx-expression-validate` | 2.0.0 | development | `(MIT AND CC-BY-3.0)` | the SPDX expression parser, whose code is MIT and whose data carries the CC-BY-3.0 of the list above |
| `spdx-ranges` | 2.1.1 | development | `(MIT AND CC-BY-3.0)` | the same, for the ranges of the SPDX list |
| `lru-cache` | 11.5.3 | development | BlueOak-1.0.0 | permissive and OSI-approved, with no copyleft term and no term beyond attribution; it is not on the allowed list only because nothing had brought one before |
| `minimatch` | 10.2.6 | development | BlueOak-1.0.0 | the same licence and the same reason |
| `@csstools/color-helpers` | 6.1.1 | development | MIT-0 | MIT with the attribution requirement waived: strictly more permissive than MIT, which is on the list |
| `@csstools/css-syntax-patches-for-csstree` | 1.1.14 | development | MIT-0 | the same |
| `argparse` | 2.0.1 | development | Python-2.0 | SPDX `Python-2.0` and SPDX `PSF-2.0` are **different identifiers** — the first is the CNRI-era Python 2.0 licence, the second the PSF licence agreement — so the allowed list's PSF-2.0 does not cover this and a row is the honest way to record it. Both are permissive, non-copyleft and Apache-compatible in the ASF's own category A. The argument parser `js-yaml` uses, brought by ESLint |

### What the bundle's record does not see

`rollup-plugin-license` reads **rollup's module graph**, so it names every
package a *module* was imported from. A package reached only through a
stylesheet is not in that graph, and is therefore **not** in
`frontend/bundled-packages.txt`:

- `@import "some-package"` or `@import "some-package/theme.css"` in a `.css`;
- `url(some-package/logo.png)` and the other places CSS names a file;
- the `@import`s inside a package's own stylesheet, once one is reached.

Those packages are still **held to the allowed list**, because
`frontend/scripts/check-licences.mjs` holds every package the lockfile pins,
whatever route it takes into the bundle — so nothing outside this policy can
be installed at all, and the licence of anything a stylesheet reaches has
already been read.

### The by-hand list

What no scanner finds, a person writes down. `CSS_PACKAGES` in
`frontend/vite.config.ts` names the packages whose CSS ships although no
module was ever imported from them, and **that list is maintained by hand**:

- **a change that adds a CSS `@import` or a `url()` naming a package adds the
  package's name there**, and that name is what a reviewer looks at, exactly
  as a new line in `bundled-packages.txt` is. Say so in the pull request too.

Once a name is written down, the build does the rest of it, and every part
fails the build rather than being skipped:

- it reads the package's own `package.json` and holds its licence to the
  **allowed list** — no exception row reaches here, because what ships in the
  bundle is the allowed list or nothing;
- it puts `name version licence` into `frontend/bundled-packages.txt`,
  alongside the module graph's, so the record is one list however a package
  got into the bundle and a version bump shows up in the diff;
- it appends the package's `LICENSE` text to `dist/THIRD_PARTY_LICENSES.txt`,
  which the wheel carries, so code that ships is not distributed without its
  notice.

`frontend/src/test/build-rules.test.ts` drives all of that against a made-up
package directory, and checks the real tree against the list.

What is still lost is what a list written by hand always loses: **a name
nobody wrote down**. A package scoped `development` in the lockfile and
excepted in the table below on that basis, which a stylesheet then pulls into
the bundle without the list being updated, is in no gate's way. The exception
says it ships in no artifact; the CSS would make that untrue.

A scanner that read the stylesheets was written for step 17 and dropped: it
has to agree with Vite's own resolver about what a specifier means — `exports`
maps, a file beside the stylesheet versus a package of that name, what Vite
rewrites inside a string — and ten rounds of review did not get it there. The
build still refuses what it can judge without resolving anything: CSS Modules,
any stylesheet language but plain `.css`, and a `<style>` block in
`index.html`. A scanner that agrees with the resolver is future work; until
then, the by-hand list is the record.


## Known exclusions

| package | licence | consequence |
|---|---|---|
| `psycopg`, `psycopg-pool` | LGPL-3.0-only | not used; the PostgreSQL driver is `asyncpg` |
| `langgraph-checkpoint-postgres` | MIT, but depends on `psycopg` | cannot be adopted as it is ([ADR 0002](docs/adr/0002-conversation-persistence.md)) |
| `tiktoken` | MIT by its LICENSE file, but its metadata puts the licence *text* in the `License` field and states no identifier anywhere | the gate fails closed and cannot classify it |
| `regex` | `Apache-2.0 AND CNRI-Python`, and CNRI-Python is on no list above | not classifiable under the policy as it stands; adding a licence to the allowed list is a decision for a person, not for a build |
| `langchain-openai` | MIT, but requires `tiktoken`, which requires `regex` | not adopted: the LangGraph engine offers the `anthropic` and `anthropic-compatible` kinds alone. OpenAI and any other OpenAI-compatible endpoint go through this client ([docs/specs/agents.md](docs/specs/agents.md)) and wait for a tree that passes. **OpenRouter does not wait for it**: it also serves Anthropic's Messages API, so it is reached through the `anthropic-compatible` kind with the Anthropic client this build already has |
| `pydantic-ai-slim[openai]` | MIT, but the extra requires `tiktoken`, which requires `regex` | the same tree, and the same answer: the Pydantic AI engine offers the same two kinds. Only the `anthropic` extra is installed, so the exclusion costs the build nothing but a vendor reachable over OpenAI's protocol alone |
| `mcp` (the MCP Python SDK) | MIT, but requires `pyjwt[crypto]`, which requires `cryptography`, which requires `cffi`; and `pywin32` on Windows | not adopted (checked 2026-09-28, at 2.2.0). The rest of its tree passes -- `mcp-types`, `jsonschema` and what it brings, `pyjwt`, `sse-starlette`, `python-multipart`, `cryptography` (`Apache-2.0 OR BSD-3-Clause`), `pycparser` (BSD-3-Clause), and what is in the lock already -- and the two below do not. So the MCP adapter is a **client of our own over `httpx`** for the three calls a client needs -- `initialize`, `tools/list`, `tools/call` -- over Streamable HTTP ([docs/specs/agents.md](docs/specs/agents.md), "Tools"), which is the fallback the plan named for exactly this case ([docs/working-notes/mcp-plan.md](docs/working-notes/mcp-plan.md), step 2). The import rule confining the SDK to `adapters/tools/mcp/` is written all the same, before the fact, so that the day the tree passes it belongs there and nowhere else |
| `cffi` | `MIT-0` (the MIT No Attribution licence), stated as its `License-Expression` since 2.1.0 (2026-07); 2.0.0 still states `MIT` | on no list above. It is strictly more permissive than MIT, the ASF category model this document adopts lists it under Category A, and the JavaScript table below already carries it twice for that reason -- so the edit is one identifier on the allowed list, and it is still a decision for a person, not for a build ([docs/working-notes/mcp-plan.md](docs/working-notes/mcp-plan.md), "Open"). It has not been taken. Brought by `cryptography`, and through it by anything that needs `pyjwt[crypto]` |
| `pywin32` | the classifier `License :: OSI Approved :: Python Software Foundation License` and `PSF` in the free-text field, which name a family and no licence | the gate fails closed and cannot classify it. Windows-only (`sys_platform == "win32"`), which the gate deliberately does not read: the locked set is checked whole, whatever this machine installs |
| `pgserver` | no licence metadata published: the wheel carries the Apache-2.0 text as its `LICENSE` and states no identifier anywhere, so the gate fails closed | not a dependency, and cannot become one. The tests take the URL of a PostgreSQL they are given, and `demo/` runs this as a **tool** in a throwaway environment (`uv run --with pgserver==<version>`), as `reuse` and `pip-audit` are run: nothing imports it and it is not in `backend/uv.lock` ([demo/README.md](demo/README.md)) |
