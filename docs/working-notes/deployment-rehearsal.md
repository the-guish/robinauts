# Deployment rehearsal — 2026-09-23

[docs/deployment.md](../deployment.md) is the guide. This note is the
record of walking it on a development machine, and of the one place a
reader should look for **what is left for the real deployment**.

Run it again with:

    scripts/rehearse-deployment.sh [work-directory] [admin-database-url]

It builds the wheel, makes a virtual environment and installs it, creates
a throwaway role and database, runs `robinauts db init`, makes a
self-signed certificate, and hands over to `scripts/rehearse_deployment.py`
for everything that needs a browser. It drops the database on the way out,
however it ends. `rehearse_deployment.py` is linted by
`scripts/check-lint.sh`, which reads `scripts/` as well as the backend;
`rehearse-deployment.sh` is linted by **nothing** — there is no shellcheck
in this repository, for any script — and no check runs either of them.

What it leaves behind in the work directory, on purpose, for reading
afterwards: `wheel/` (the wheel and its `requirements.txt`), `venv/`,
`tls/`, the generated `robinauts.toml` and `server.log`. Only the database
and the role are put back. Remove the directory when you are done with it.

## What stood in for what

| the real thing | the rehearsal |
|---|---|
| Google and Okta | two **stand-in OIDC providers** (`backend/tests/standin`), one per person, on loopback ports the operating system picked. Each runs the whole authorization-code flow — state, nonce, PKCE, the code exchange, an ID token — so what the platform does is unchanged |
| a domain name and a certificate | `https://127.0.0.1:8443`, self-signed with `openssl` |
| nginx or Caddy | a ~60-line TLS terminator inside the driver: it terminates TLS, sets `X-Forwarded-For`, `X-Forwarded-Proto: https` and `X-Forwarded-Host`, and pipes everything else through. The server ran with `--forwarded-allow-ips 127.0.0.1`, as the guide's unit does |
| an Anthropic key | an invented one, distinctive so that a grep for it means something. Turns therefore fail at the vendor with a real 401 |

`public_url` on `http` is *not* refused on a loopback host
(`core.normalise_origin`), so a plain-http rehearsal was possible — and
useless, because the `__Host-` prefix and `Secure` come off with it and
the cookie path is then not the deployed one. Hence the terminator.

The machine already had something listening on `127.0.0.1:8000`, so the
run used `ROBINAUTS_REHEARSAL_PORTS="8010 8443"`. The driver refuses to
start when a port is taken rather than proxying to whatever is there — the
first attempt did exactly that and reported on a stranger's agent list,
which is what the check was added for.

## What was rehearsed, and what it said

32 of 32 outcomes green, exit 0. The whole log is reproducible; the
substance:

**The wheel and the machine.** `scripts/build-wheel.sh` built
`robinauts-0.1.0-py3-none-any.whl` (694 KB) with the interface in it, and
`requirements.txt` beside it — 56 runtime packages, pinned, with hashes.
`python3 -m venv`, then `pip install --require-hashes -r requirements.txt`
and `pip install --no-deps <wheel>`; `robinauts version` printed
`robinauts 0.1.0 (schema 2)`.

**That second file earns its place.** The first rehearsal installed the
wheel alone, and `pip` resolved its ranges against PyPI on the day:
`anthropic 1.8.0` where the lock pins `1.7.0`, `starlette 1.7.0` where it
pins `1.6.0`, and the same for `langchain-anthropic` and
`pydantic-ai-slim`. Four packages the licence gate and `pip-audit` had
never looked at, in a deployment nobody had done anything wrong in.
Installing from the export gives the locked versions exactly.

**The database.** A role and a database created with two statements (this
cluster has no `psql`, so the driver in the wheel ran them);
`robinauts db init` printed `the database is at schema version 2`, and
printed the same on a second run.

**Start-up refusals**, each a process of its own, each exit 2, no
traceback:

- no `ROBINAUTS_CONFIG` → `no configuration: set ROBINAUTS_CONFIG to the
  TOML file describing this deployment`;
- no `ROBINAUTS_DATABASE_URL` → `no database: set ROBINAUTS_DATABASE_URL
  to the PostgreSQL this deployment uses`;
- all three secret variables unset → **all three named in one refusal**,
  one per line, by variable name and never by value.

**The interface, out of the wheel.** `/` → `302 /ui/`; `/ui/` → 200 with
the platform's own `Content-Security-Policy`.

**Sign-in over https.** Two people signed in through two different
providers, one matched by `email` and one by `group` (with `groups_claim`
and the `groups` scope). The session cookie really was
`__Host-robinauts_session`. A third person, whom the provider
authenticated perfectly well, landed on
`https://127.0.0.1:8443/ui/#/sign-in?error=not_allowed` and was signed in
to nothing; the provider's own words stayed in the server's log.

**Private conversations.** A turn begun by the first person created a
conversation; its author opened it (200); the other person got **404**,
not 403, and it was not in their history.

**The stream.** The stream of a started run was read, dropped after its
first event, and re-attached with `Last-Event-ID: 1`; the second request
replayed nothing before position 1.

**The configuration across a restart.** The agent's `engine` was changed
from `langgraph` to `pydantic-ai` in the file and the server restarted:
`/api/agents` then reported `pydantic-ai`, and the conversation begun
under the first engine was still there and still the same person's.

**The secrets, nowhere.** Three places, the same three secrets. Not in any
line of the server's log — including the line where a turn failed, where
the vendor's own answer is quoted ("API key is invalid") and the key is
not. Not in any of the **answers** the driver read: every response it
fetched is kept, headers and body, the interface page, `/auth/session`,
`/api/agents`, the conversation, the history and both event streams
included. And not in any database row: every row of all eight tables was
dumped with `to_jsonb` and searched. The database **password**, and the
whole url it lives in, are in neither the log nor a response — it is the
credential that matters there, not the host and the database name after the
`@`, which a message about a database may perfectly well say
(`domain.without_secrets`).

## The GPL gate (goal 7)

- **JavaScript, live.** A `GPL-3.0` package added to a scratch copy of
  `frontend/package-lock.json` turned `node scripts/check-licences.mjs`
  red: `robinauts-gpl-probe 1.0.0 is GPL-3.0, which DEPENDENCIES.md
  forbids (GPL-3.0); no row may except it`, exit 1. The lockfile was
  restored from the copy afterwards and is untouched.
- **Python, live.** `pylint 4.0.8` (`License-Expression:
  GPL-2.0-or-later`) added to a scratch copy of `backend/uv.lock`, with its
  real metadata in the environment the gate reads, turned
  `scripts/licence_gate.py` red:

      FAIL pylint                GPL-2.0-or-later  [runtime, from gplenv]
      1 package(s) fail the policy of DEPENDENCIES.md: pylint

  exit 1. Nothing in the repository was touched: the lock was a copy, and
  the environment was a directory of `.dist-info/METADATA` files. In CI it
  would not even get that far — the `dependency licences` job runs
  `uv sync --locked` before `scripts/check-licences.sh`, and a lock that
  does not match `pyproject.toml` is refused there. The verdict table
  itself is covered by `backend/tests/unit/test_licence_gate.py`, which
  asserts that `GPL-*`, `LGPL-*` and `AGPL-*` are forbidden, inside
  `AND`/`OR` expressions too, and that a policy document doctored to allow
  them is refused.

## The seven "Done when" items

[poc-scope.md](poc-scope.md) lists them. Where each stands:

| # | what it asks | status |
|---|---|---|
| 1 | two people sign in, Google and Okta, over https; a third is refused | **rehearsed** against two stand-in providers over https, with the `__Host-` cookie and the `not_allowed` page. **Needs the real deployment** for Google and Okta themselves: their client registrations, the redirect URIs, and Google's `hd` rules |
| 2 | each sees only their own conversations | **rehearsed** in full: 404 by id, and absent from the other's history |
| 3 | engine and vendor swapped in the configuration, across a restart | **partly**. The **engine** swap across a restart is rehearsed (`langgraph` → `pydantic-ai`, the conversation survives it); a model key would finish it by having the next turn really answer. The **vendor** swap could not be done at all when this was rehearsed: the build reached Anthropic alone, because no other provider's client passes `DEPENDENCIES.md`. **Since then** the `anthropic-compatible` kind was added, which reaches OpenRouter with the Anthropic client (`docs/specs/legacy/agents-engines-models.md`), so there are now two vendors to swap between and only a key stands in the way. This was not rehearsed again |
| 4 | tab closed mid-answer, re-attach | **partly**: the stream, the drop and the re-attach by `Last-Event-ID` are rehearsed, on a run that fails rather than answers. A long answer to watch arrive **needs a model key** |
| 5 | keys removed → start-up names the variables; keys nowhere | **rehearsed** in full: every variable named at once, and no secret in any log line, in any of the answers the driver read, or in any database row |
| 6 | a clean machine from nothing, by the guide alone | **partly**: every step of the guide was run here, in its order, from a wheel into an empty virtual environment. This machine is not clean, there is no systemd unit and no nginx or Caddy on it, so the unit and the two proxy snippets are **prose that has not been run**. **Needs the real deployment** |
| 7 | CI green, and a GPL dependency makes it red | **rehearsed** live for both gates: exit 1 each, naming the package and the licence. CI being green is CI's to say |

## What is left for whoever does the real deployment

1. A machine, a host name and a certificate; nginx or Caddy in front, from
   the snippets in the guide — **the first thing to check**, since they
   have not been run anywhere.
2. The systemd unit, likewise: `EnvironmentFile` at mode 0600,
   `RestartPreventExitStatus=2`, `TimeoutStopSec` over a minute, and —
   if the socket is used — `RuntimeDirectory=` with the group recipe.
3. Google and Okta client registrations, and the two redirect URIs
   pasted in exactly. For Okta: the groups claim, the `groups` scope,
   assignment of the two people to the application.
4. A real model provider key: hold a conversation, change the agent's
   engine, restart, and carry the same conversation on; and close a tab in
   the middle of a long answer. That finishes item 4 and the engine half of
   item 3.
5. The **vendor** half of item 3 waited on something else entirely when this
   was written: the build reached Anthropic alone, because no other
   provider's client passes `DEPENDENCIES.md`. The `anthropic-compatible`
   kind has since been added and reaches OpenRouter, so this now needs a key
   like the rest — one for each of the two vendors — and no longer a
   dependency decision.
6. Take **both** files of the artifact, and install the
   `requirements.txt` before the wheel. Installing the wheel alone is a
   deployment running versions no gate has judged.
7. Write down what broke. This note is where it goes.

## What broke

**Nothing in the platform's own behaviour.** No message was wrong, no flag
was missing, and nothing in `backend/src` needed a change.

**One thing in the delivery, and it was real.** `pip install
robinauts-*.whl` resolves the wheel's dependency ranges against PyPI on the
day, so what a deployment runs need not be the set `backend/uv.lock` pins
— the set the licence gate and `pip-audit` judged and the tests ran
against. The first rehearsal drifted on four packages by itself. Fixed
here: `scripts/build-wheel.sh` now exports that locked runtime set, with
hashes, as `requirements.txt` beside the wheel; `scripts/check-wheel.sh`
refuses a release directory without it; CI uploads both files in the
artifact; the guide installs from it and then the wheel with `--no-deps`;
and `backend/tests/integration/test_wheel_contents.py` reads the export
command **out of the build script** and holds what it produces to
`uv.lock`, to `pyproject.toml`'s runtime dependencies, and to having a hash
on every pin.

Two things about the rehearsal itself, fixed in the scripts:

- the driver first proxied to a server that was already on port 8000 and
  cheerfully reported its agents. It now refuses a taken port, and watches
  the server process as well as the port while waiting for `/health`;
- the stand-in providers were built with their default client secret while
  the configuration named another, so every sign-in failed
  `provider_refused`. The guide gained a row for that error as a result,
  since "the wrong client secret" is exactly what it will mean on a real
  machine.

The rehearsal's role is `robinauts_rehearsal`, not `robinauts`: the script
drops the role it made, and the guide's own role is called `robinauts`.

Corrections to the guide, all made before it was finished:

- an early draft said the deployment is https because the proxy sends
  `X-Forwarded-Proto`. It is not: `Secure` and `__Host-` come from
  `public_url` beginning `https://`, and the origin check compares the
  browser's `Origin` with that same string. Nothing in the platform reads
  a request's scheme or client address, and what `--forwarded-allow-ips`
  really decides in this build is the address in the access log. The
  guide and three rows of its table say that now;
- `--uds` under `ProtectSystem=strict` needs `RuntimeDirectory=`, or
  `/run` is read-only and the socket cannot be bound at all; and a proxy
  running as another user needs the whole recipe —
  `RuntimeDirectoryMode=0750`, `--uds-mode 0660`, and the proxy's account
  in the `robinauts` group — which the guide now gives;
- `Restart=on-failure` without `RestartPreventExitStatus=2` restarts for
  ever on a configuration refusal, which exits 2;
- the schema is created **after** the environment file exists, not beside
  `createdb`: `ROBINAUTS_DATABASE_URL` is never a command-line flag, so
  `db init` is run through the same `EnvironmentFile`;
- what a good start really prints is five `info` lines, which is what the
  guide now lists;
- the shutdown bound is about 45 s (10 + 35), not 45 written as 35.
