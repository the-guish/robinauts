# Contributing to Robinauts

Robinauts is Apache-2.0 and means to stay safe to run inside a company and
safe to embed in a commercial product. Most of what follows exists to keep
that true: for any line in the tree, it must be possible to show who wrote
it, under what licence, and that they had the right to. The rules are
summarised in [docs/specs/open-source.md](docs/specs/open-source.md).

## Branching

`main` is the only long-lived branch and must always be releasable.
Changes reach it through pull requests only, and it is never force-pushed.

1. Create a short-lived branch from `main`, prefixed `feature/` or `fix/`.
2. Open a pull request against `main`.
3. Once the checks pass and the review is complete, a maintainer merges it.

One concern per pull request. A change of dependency is a pull request of
its own.

## Repository layout

    backend/     the Python backend (the `robinauts` package)
    frontend/    the web UI (Vite, React, TypeScript)
    channels/    the bridge to chat platforms, a container of its own (Node, TypeScript)
    docs/        specs, decisions, legal records, working notes
    scripts/     the checks, one script per gate, which CI runs as they are

The backend is split into layers with enforced dependency rules. Read
[docs/layout.md](docs/layout.md) before adding a module: where code goes is
not a matter of taste here, and the test suite fails on an import that
crosses a layer the wrong way.

## Development setup

The backend uses [uv](https://docs.astral.sh/uv/). From `backend/`:

    uv sync

## Checks

Every check is a script in [scripts/](scripts/), and CI runs those same
scripts, one per job and nothing else, so a check can be run and debugged
where it fails. CI is not your machine, though: it installs the pinned uv
from [scripts/tool-versions.sh](scripts/tool-versions.sh) and runs on Python
3.12, so a failure that depends on a version may show up in one place and not
the other. Run them all before opening a pull request, from the root of the
repository:

    scripts/check-all.sh

or one at a time:

    scripts/check-lint.sh       ruff and black, over backend/ and scripts/
    scripts/check-tests.sh      pytest, with the architecture contracts
    scripts/check-licences.sh   the dependency licence gate of DEPENDENCIES.md
    scripts/check-audit.sh      pip-audit over the whole locked set
    scripts/check-frontend.sh   the frontend: format, lint, types, tests,
                                the build with its licence gate, npm audit
    scripts/check-channels.sh   the bridge to chat platforms: format, types,
                                tests, npm audit
    scripts/check-e2e.sh        the interface in a browser (Playwright),
                                over a server on both agent engines and
                                PostgreSQL: the one ROBINAUTS_TEST_DATABASE_URL
                                names, or a throwaway one in Docker
    scripts/check-wheel.sh      the wheel: built, looked inside, installed
    scripts/check-reuse.sh      reuse lint: every file states its licence
    scripts/check-dco.sh        sign-off on every commit of the branch

Every one of them runs on every pull request, and a failure is a failure:
none of this is advisory. Making them *required* — so that GitHub refuses the
merge button rather than showing a red cross — is branch protection, which
the project owner still has to switch on
([docs/oss-checklist.md](docs/oss-checklist.md)).

`check-tests.sh`, `check-e2e.sh`, `check-licences.sh` and `check-reuse.sh`
pass their arguments on to the tool they wrap, so
`scripts/check-tests.sh -k licence` does what you would expect.
`check-dco.sh` takes a commit range and defaults to what this branch adds to
`main`; CI runs it over the commits of the pull request, since the first
commits of this repository predate the sign-off rule. `check-lint.sh`, `check-audit.sh`, `check-frontend.sh` and
`check-all.sh` take no arguments and say so rather than ignoring them —
`check-audit.sh` reads pip-audit's JSON to make sure every pinned package was
really looked at, and `check-frontend.sh` is the frontend's whole CI job in
order; an argument that changed either would quietly turn the check off.

### The wheel

A deployment is one wheel and one PostgreSQL
([docs/specs/operations.md](docs/specs/operations.md)), and the wheel carries
the **built frontend**: `frontend/dist` goes into the package as
`robinauts/ui/`, which is what the backend serves under `/ui/`. So building
one means building the interface first, which is what
`scripts/build-wheel.sh` does:

    scripts/build-wheel.sh /tmp/robinauts-wheel   # prints the path it built

`scripts/check-wheel.sh` is the gate around it: it builds the wheel, looks
inside it for the interface, the schema and the three licence files, installs
it into an empty virtual environment and runs `robinauts version` out of it.
It works in a temporary directory and leaves the checkout alone. It takes a
few seconds beyond the frontend build and is part of `check-all.sh`; in CI it
is the `wheel` job, and the artifact of a green run is what gets installed
(there is no release workflow yet).

**A wheel cannot be built without the interface.** `backend/hatch_build.py`
refuses when `frontend/dist` has no `index.html`, or no
`THIRD_PARTY_LICENSES.txt` beside it — one would install and serve a page
saying the interface is not built, the other would redistribute other
people's code without their notices. An **editable** install (`uv sync`) is
exempt and is the only thing that is: a development checkout has no built
frontend, the interface is developed against Vite's own server, and `/ui/`
then answers the page that explains it.

**Use `uv build --wheel`, through the script, and not plain `uv build`.** The
source distribution is a copy of `backend/` and carries no interface, because
the interface is not under `backend/`; a wheel built *from* an unpacked sdist
therefore cannot carry one either, and is refused with a sentence saying so.
`uv build` with no argument does exactly that — sdist, then wheel from the
sdist — so it fails, on purpose. The sdist itself builds, and is not the
deliverable. Nothing is left out of it beyond what is not in `backend/` in
the first place.

**Build one at a time in one checkout.** The licence files the build stages
beside `pyproject.toml` are real files, so two builds of this project running
at once would share them and the first to finish would take them out from
under the second. Only a checkout is written to: in an unpacked sdist the
files of those names are ones the distribution carries, and nothing here
touches them.

One script under `scripts/` is not a gate: `scripts/update-openapi.sh`
rewrites `backend/openapi.json` from the routes as they are. The document is
committed, and `check-tests.sh` fails when the file and the code disagree, so
a change to the wire is something a reviewer reads in the diff. Run it after
changing a route, and read what it wrote.

`reuse` and `pip-audit` are not dependencies of the project: they run as
isolated tools, from a version pinned in
[scripts/tool-versions.sh](scripts/tool-versions.sh) and bumped by hand. That
file is the one place for such pins, and CI reads uv's version from it too.
[DEPENDENCIES.md](DEPENDENCIES.md) says why they stay out of the lockfile.

### PostgreSQL for the tests

Most of the suite needs nothing but Python. The tests under
`backend/tests/adapters_free/` and `backend/tests/api/` need a PostgreSQL, and they are **given** one
rather than starting one: set `ROBINAUTS_TEST_DATABASE_URL` to its URL and
run the tests as usual.

    ROBINAUTS_TEST_DATABASE_URL=postgresql://user@localhost/robinauts_test \
        scripts/check-tests.sh

Any PostgreSQL 14 or later will do, on your machine or anywhere you can
reach it, as long as the account may create and drop schemas in that
database: each test makes a schema of its own, named after a fresh UUID, sets
the connection's `search_path` to it, and drops it when it ends. That is also
how a deployment is expected to be set up — the schema the tables live in is
the first entry on the path — and `check_schema` refuses to start when it is
not.

Nothing is left behind, two runs at once do not interfere, and no test needs
a database to itself. CI runs them against a service container, which is the
same arrangement — and in a time zone that is deliberately not UTC, so that a
store reading a time as a wall clock fails there rather than in somebody's
deployment.

Without the variable those tests **skip**, with the reason printed, and
everything else runs — so a contributor with no PostgreSQL to hand still
gets a green `scripts/check-all.sh`. They are marked `io`, so
`scripts/check-tests.sh -m "not io"` skips them even when the variable is
set.

Set `ROBINAUTS_REQUIRE_POSTGRES=1` and a missing database becomes a
**failure** instead of a skip. CI sets it, because a skip that nobody sees is
how a typo in the URL, or a service container that never started, quietly
stops the database tests from running while the build stays green. It is
checked at the end of the run, on what actually happened: a run with the
variable set in which no database test ran is failed, and so is one narrowed
with `-m`, `-k` or a path argument, since such a run cannot show anything
about the tests it did not select. Narrow your runs freely — just without
the variable. A run split across processes with `-n` is refused for the same
reason rather than guessed at: the count is kept in one process, and
`pytest-xdist` is not a dependency of this project. Unset, empty, `0`, `false`, `no` and `off` all mean "a skip is
fine".

No package that starts a PostgreSQL is a dependency of this project;
[DEPENDENCIES.md](DEPENDENCIES.md) says why.

### Changing the database schema

`backend/src/robinauts/controller/adapters/postgres/schema.sql` is the whole schema, and until
the first release it is **one definition edited in place**: there are no
migrations, and a database made from an older definition is recreated rather
than upgraded. So
`robinauts db init` applies the file to an **empty** database, does nothing
to one already at this version, and refuses every other database there is.
Every edit to that file updates the `SCHEMA_SHA256` pinned in
`controller/adapters/postgres/schema.py`; `SCHEMA_VERSION` stays at 1, and a test enforces
both. That pin is the only thing standing where a migration would otherwise
be; freezing the released schema comes with the migrations work. After
pulling a schema change, drop your local database and run
`robinauts db init` again. If you forget, the server and the command refuse
the database, saying it was made from an older `schema.sql`: `db init`
records the file's hash in the version row and both compare it, since the
version alone says nothing about which edit a database was made from. The
tests are unaffected, because each creates a fresh schema. The file is
hashed with `\n` line endings, which
[.gitattributes](.gitattributes) keeps it checked out with everywhere.

Constraint names in that file are part of its interface: the store turns a
violation of `sessions_secret_hash_key` or `sessions_user_id_fkey` into an
answer for its caller and lets every other one through as the bug it is, and
it tells them apart by name. Renaming one without changing the store fails a
test.

## Licence header

Every source file starts with:

    # SPDX-License-Identifier: Apache-2.0
    # Copyright The Robinauts Authors

in the comment syntax of its language. A file that cannot carry a header is
covered by [REUSE.toml](REUSE.toml), which lists the files at the root by
name: a new file there is added to it. `reuse lint` must pass: every file
in the tree has a stated licence.

Never remove or alter a header that came with someone else's file.

## Developer Certificate of Origin

All contributions must be signed off under the
[Developer Certificate of Origin 1.1](https://developercertificate.org/).
Add `-s` to your commit:

    git commit -s -m "component: short imperative summary"

which appends:

    Signed-off-by: Jane Doe <jane@example.com>

Use your real name and a reachable email address. Sign-off is a statement about
the provenance of your contribution, so pseudonymous sign-offs cannot be
accepted. If you are contributing on behalf of an employer, make sure you have
their authorization.

`scripts/check-dco.sh` checks this, and CI runs it on every pull request. The
sign-off may be the author's or the committer's: the DCO is certified by
whoever puts the commit here, and that is not always the person who wrote it.

A dependency update opened by Dependabot carries no sign-off, because a bot
certifies nothing. A maintainer takes such a change over before it is merged,
by one of:

    git cherry-pick -s <commit>          keeps Dependabot as the author and
                                         signs off as the committer
    git commit --amend -s --reset-author makes it your commit outright

Both pass the check. `git commit -s` on top of an unsigned commit does not:
the commit without the trailer is still in the range.

There is no contributor licence agreement. You keep the copyright in your
contribution and license it to everyone under Apache-2.0.

## AI-assisted contributions

AI coding assistants are allowed. By signing off, you certify that:

- you have reviewed and understand every line you submit;
- the contribution does not reproduce third-party code under a license
  incompatible with Apache-2.0, and does not carry attribution requirements
  we have not satisfied;
- you have disclosed in the PR description any substantially machine-generated
  file or block.

## Where code may come from

- Write it yourself, or take it from a source whose licence is on the
  allowed list in [DEPENDENCIES.md](DEPENDENCIES.md) — and then say so:
  keep the copyright line and the header that came with the file, add the
  text of its licence to [LICENSES/](LICENSES/) if it is not there yet, and
  record it as described below.
- Earlier work of your own is yours to contribute, published or not, as
  long as you alone hold the rights to it — no co-author, no employer with
  a claim. Signing off is your statement that this is so.
- **No code from Stack Overflow, blogs or forums.** Their licences are
  usually incompatible. Understand the idea, close the tab, write your own.
- **Nothing copied, ported or translated** from someone else's work that is
  GPL, AGPL, LGPL, proprietary, or has no licence at all. A repository without a
  licence file is "all rights reserved", not public domain.
- Code that enters the tree other than as your own work in an ordinary
  signed-off pull request — a vendored file, a component copied from a
  registry, code derived from another project, an asset — gets an entry in
  [docs/legal/ip-clearance.md](docs/legal/ip-clearance.md), in the same
  pull request. Third-party code and assets that stay in the tree are also
  listed in [docs/legal/third-party.md](docs/legal/third-party.md).
- Do not import a codebase in one commit.

## Dependencies

Adding or upgrading a dependency follows [DEPENDENCIES.md](DEPENDENCIES.md).
In short: its licence, and the licence of everything it brings, must be on
the allowed list; the pull request says why the dependency is needed; and
the lockfile is committed. `scripts/check-licences.sh` enforces the licence
rules over the whole locked set, and a licence it cannot resolve fails as
surely as a forbidden one.

npm has rules of its own, because its packages are compiled into a bundle
that ships inside the wheel:
[docs/contributing/js-dependencies.md](docs/contributing/js-dependencies.md).
Read it before touching anything under `frontend/`. There are two gates.
`frontend/scripts/check-licences.mjs` applies the policy of
[DEPENDENCIES.md](DEPENDENCIES.md) to **everything the lockfile pins and npm
installed**, with the development exceptions named — by package, version,
scope and licence — in that document's "JavaScript build tooling" table, and
it refuses a version in `package.json` that is not exact. The build then
gates the **bundle** itself: what ends up in it, and nothing else, has to be
on the allowed list with no exception at all, and
`frontend/bundled-packages.txt` records what that was, written by the build
and compared with the commit. `scripts/check-frontend.sh` runs both.
