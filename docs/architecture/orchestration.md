# Orchestration

One of Robinauts foundationals premises is to be well architected to support multiple deployment modes with the same code base, and to be modular enough to be partially adopted as component on other systems. It aims to be deployable as a multi-user platform for a company, but also be fully reused as a desktop application.

[Neorc](https://github.com/open-shipyard/neorc) aims to be lightweight orchestration layer with minimal dependencies that also can be deployed to cluster or be embedded in smaller applications.


They integrate together to server multiple scenarios.

On dev mode or desktop application, Robinauts starts a local instance of Neorc that users can take for orchestrating their own local workflows.


On a cluster Neorc would be started on its own and connected to a running instance of Robinauts, creating sessions with a system user provided by Robinauts.


## Local development: `robinauts start` starts neorc

An `[orchestration]` table in the configuration turns it on. `robinauts start
--dev-no-sign-in` then starts neorc beside itself, before the server listens,
and stops it with the server ([cli/orchestration.py](../../backend/src/robinauts/cli/orchestration.py)):

1. neorc's **manager**, on `127.0.0.1` with no authentication, as the local
   development mode is; its database is the PostgreSQL that the
   `database_url_env` variable names (`NEORC_DATABASE_URL` by default);
2. once it answers `/health`, the **upload** of the flows in `flows`;
3. the **scheduler**;
4. one **worker**, on the `default` queue, with the handlers in
   `code_location`.

The stop goes the other way round: worker, scheduler, manager, each with
SIGTERM and killed after fifteen seconds. If any of the first steps fails, the
start is refused and nothing listens.

```toml
[orchestration]
command = ["neorc"]                       # how neorc is run; the default
port = 8420                               # the manager's port; the default
database_url_env = "NEORC_DATABASE_URL"   # the default
code_location = "orchestration"           # the handlers; default: this checkout's orchestration/
flows = "orchestration/flows"             # the flow files; default: code_location/flows
ui = true                                 # serve neorc's UI, for the Flows page; false when it is not built
```

Relative paths start at the configuration file's directory. Robinauts does not
depend on neorc: neorc is installed on its own, and `command` says how it is
run, for instance `["uv", "run", "--project", "/path/to/neorc", "neorc"]`.

With sign-in on, the table is refused for now: neorc's manager would run with
no authentication. On a cluster neorc is started on its own.

### The orchestrator's secret

At each start the server mints a secret for the orchestrator, `rbo_` and 32
random bytes. It is never stored and dies with the process. The neorc worker
alone is handed it, in `ROBINAUTS_API_TOKEN`, with the server's URL in
`ROBINAUTS_API_URL`. Sent as `Authorization: Bearer`, it reaches every route
under `/api/`:

- in the local development mode, as its one user, whose workflows they are,
  so what a flow starts shows up in the UI;
- with sign-in on, as a user of the server's own (provider `!system`, subject
  `orchestrator`), which no identity provider can name.

It never mints or removes a credential (403), and any other `rbo_` bearer is
401.

None of neorc's processes is handed what the configuration keeps from
everything but the server: the model providers' keys, the tool servers'
secrets, `ROBINAUTS_DATABASE_URL` and `ROBINAUTS_CONFIG`.

### The Flows page

The interface frames neorc's own UI on a page of its own, "Flows", under the
conversations in the left panel. The server serves the manager it started
on its own origin, under `/neorc/` ([web/neorc.py](../../backend/src/robinauts/web/neorc.py)):
neorc's UI at `/neorc/ui/`, and its API right above it, which is where that
UI looks for it. The page frames `/neorc/ui/`; `#/flows` is neorc's list of
runs, and `#/flows/runs/<id>` is neorc's `#/runs/<id>`.

- **Only in the local development mode with an `[orchestration]` table.**
  Without it nothing answers under `/neorc/`, and `GET /auth/session` says
  `flows: false`. With `ui = false` the API is still served, and the page
  is not offered.
- **Everything streams through.** An answer is passed on as the manager
  sends it, so neorc's 20-second long polls of its event log work, and a
  client that goes away cancels the request upstream. A request body is 8
  MiB at most.
- **neorc gets none of Robinauts' credentials.** The request reaches the
  manager without `Cookie`, `Authorization` and `Origin`; the answer comes
  back without `Set-Cookie`. The `Host` and `Origin` checks of the local
  development mode apply under `/neorc/` as everywhere else.
- **Framed by this server alone.** An answer under `/neorc/ui/` keeps
  neorc's Content-Security-Policy, with `frame-ancestors 'none'` made
  `'self'` and `connect-src 'self'` narrowed to `<origin>/neorc/`, and
  carries `X-Frame-Options: SAMEORIGIN`. Every other answer under
  `/neorc/` carries the server's usual headers, `DENY` included.

#### What this costs: neorc's code on Robinauts' origin

This variant puts neorc's bundle on the same origin as the interface and
`/api/`. Any script in it, neorc's own or a package neorc bundles, runs as
this origin, and so can call `/api/` as the person using the page, with
whatever session they have. **Robinauts' dependency vetting does not cover
neorc's packages**: `frontend/`'s rules, licence gate and cooldown govern
this repository's bundle, not neorc's, which neorc vets under rules of its
own.

What holds it in, and how far:

- **Local development only.** The table is refused with sign-in on, so
  there is no session cookie to take: what a script in the frame can do is
  what any process on the developer's machine can already do with the
  loopback server.
- **The policy.** neorc's `connect-src` reaches `/neorc/` and no further,
  so its own `fetch` cannot reach `/api/`; its `script-src 'self'` and the
  rest are neorc's and stay as neorc wrote them.
- **The sandbox.** The frame is
  `sandbox="allow-scripts allow-same-origin allow-forms"`: no navigating
  the interface, no new windows, no downloads, no dialogs.
  `allow-same-origin` is what lets neorc reach its API at all, and with it a
  script in the frame can reach the parent page, whose own policy allows
  `/api/`. **The policy and the sandbox stop the obvious ways, not a
  determined script.**

Before the page could be offered beyond the local development mode, this
has to change: neorc on an origin of its own and framed across origins, or
its bundle held to Robinauts' vetting.

### The flows Robinauts ships

[orchestration/](../../orchestration/README.md) holds them, with their
handlers: plain functions that use the standard library alone and import
neither neorc nor Robinauts.

- `guess_number`: a guessing game between two conversations with the first
  agent the server offers.
