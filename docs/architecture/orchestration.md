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
   `database_url_env` variable names (`NEORC_DATABASE_URL` by default); with
   `ui`, it serves neorc's UI at `/ui/`, which only the server's own origins
   may frame (`--frame-ancestors`, below);
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
ui = true                                 # serve neorc's UI at /ui/, framed by the Flows page; false when it is not built
```

Relative paths start at the configuration file's directory. Robinauts does not
depend on neorc: neorc is installed on its own, and `command` says how it is
run, for instance `["uv", "run", "--project", "/path/to/neorc", "neorc"]`.

With sign-in on, the table is refused for now: neorc's manager would run with
no authentication. On a cluster neorc is started on its own.

### The Flows page: neorc's UI, framed on its own origin

With `ui` on, the left panel has a **Flows** item below the history, and
`#/flows` is a page of the shell that frames neorc's UI in its embedded mode
(`?embed=1`, which leaves out neorc's own left navigation). `#/flows` opens
neorc's list of runs, `#/runs`; `#/flows/runs/<id>` opens `#/runs/<id>`. What
the reader does inside the frame stays inside it: Robinauts' hash does not
follow it.

**neorc's UI stays on its own origin**, `http://127.0.0.1:<port>`, which is
another origin than the server's: another port is another origin. So nothing
neorc's page runs can read Robinauts' cookies, call `/api/` as the person
signed in, or reach into the page that frames it; and nothing of Robinauts'
reaches into neorc's either. The isolation is the reason for framing it rather
than serving it under the server's origin. Each side opens exactly one door:

- neorc's UI may be framed by the server's origins alone. The server starts
  the manager with `--frame-ancestors` for each origin a browser on this
  machine reaches it at: `http://127.0.0.1:<server port>` and
  `http://localhost:<server port>` for a server on the loopback interface or
  on every address ([cli/orchestration.py](../../backend/src/robinauts/cli/orchestration.py),
  `server_origins`). Without the option neorc keeps `frame-ancestors 'none'`.
- The server's page may frame neorc's origin and no other: it is added to
  `frame-src`, beside `'self'`, only when the orchestrator serves its UI
  ([web/headers.py](../../backend/src/robinauts/web/headers.py)). The rest of
  the policy, `frame-ancestors 'none'` and `X-Frame-Options: DENY` included,
  is unchanged.
- The frame is sandboxed with `allow-scripts allow-same-origin allow-forms`:
  neorc's scripts, its own origin (to reach its API and keep its settings),
  its forms (to start a run). `allow-same-origin` gives the frame neorc's
  origin, never Robinauts': it is cross-origin either way. No popups, no top
  navigation, no downloads, and no referrer.

`GET /auth/session` tells the interface where neorc's UI is, in `flows_url`;
it is `null`, and the panel has no Flows item, when the server runs no
orchestrator or runs it with `ui = false`. neorc's manager listens on the
loopback interface alone, so the Flows page works in a browser on the same
machine; a phone on the LAN that reaches the server gets the item but not
the frame.

`--frame-ancestors` is an option of neorc's manager that only the fork's
branch `feature/flows-cross-origin-iframe` has until it is merged and
released: point `command` at that checkout, or set `ui = false`.

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

### The flows Robinauts ships

[orchestration/](../../orchestration/README.md) holds them, with their
handlers: plain functions that use the standard library alone and import
neither neorc nor Robinauts.

- `guess_number`: a guessing game between two conversations with the first
  agent the server offers.
