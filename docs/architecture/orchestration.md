# Orchestration

One of Robinauts foundationals premises is to be well architected to support multiple deployment modes with the same code base, and to be modular enough to be partially adopted as component on other systems. It aims to be deployable as a multi-user platform for a company, but also be fully reused as a desktop application.

[Neorc](https://github.com/open-shipyard/neorc) aims to be lightweight orchestration layer with minimal dependencies that also can be deployed to cluster or be embedded in smaller applications.


They integrate together to server multiple scenarios.

On dev mode or desktop application, Robinauts starts a local instance of Neorc that users can take for orchestrating their own local workflows.


On a cluster Neorc would be started on its own and connected to a running instance of Robinauts, creating sessions with a system user provided by Robinauts.


## Local development: neorc inside `robinauts start`

An `[orchestration]` table in the configuration turns it on. `robinauts start
--dev-no-sign-in` then embeds neorc in its own process and on its own port
([cli/orchestration.py](../../backend/src/robinauts/cli/orchestration.py)):

1. neorc's **manager** is an application mounted at `/neorc`, its UI at
   `/neorc/ui/`, with no authentication of its own, as the local development
   mode has none. Every request to it passes this server's checks first: the
   mode's `Host` and `Origin` rules reach `/neorc` as they reach every path;
2. its **storage** follows this server's: in this process without
   `ROBINAUTS_DATABASE_URL`, gone at the stop; on that PostgreSQL with it, in a
   schema of its own, `neorc`, made at the start if it is not there. Its tables
   are named `neorc_*` besides, so none meets one of this server's;
3. the flows in `flows` are **uploaded**, at every start;
4. its **scheduler** runs in this process, on the manager directly;
5. one **worker**, on the `default` queue, with the handlers in
   `code_location`, runs in a process of its own: neorc's own command, as
   installed with it, against `http://127.0.0.1:<port>/neorc`. It waits for the
   server to listen, as a neorc worker waits for any manager.

All of it starts after the server's own workers and before the server
listens, and stops before the server stops listening: the worker with SIGTERM,
killed after fifteen seconds, then the scheduler, then the storage. If the
storage does not open, the flows are refused or the worker cannot be started,
the start is refused and nothing listens.

```toml
[orchestration]
code_location = "orchestration"           # the handlers; default: this checkout's orchestration/
flows = "orchestration/flows"             # the flow files; default: code_location/flows
ui = true                                 # serve neorc's UI at /neorc/ui/; the default
```

Relative paths start at the configuration file's directory. The table's
`command`, `port` and `database_url_env`, from when neorc ran beside the server
as processes of its own, are refused with the reason: there is no second port
and no second database any more.

With sign-in on, the table is refused for now: neorc's manager would be served
with no authentication. On a cluster neorc is started on its own.

### Why the worker is a process of its own

The manager and the scheduler are neorc's own code deciding about flows; the
worker runs the handlers, which are whatever `code_location` holds. Three
things put it outside the server's process:

- neorc puts the code location first on `sys.path`, so a handler module could
  shadow a package the server imports later;
- the handlers read `ROBINAUTS_API_URL` from their environment, and the
  server's holds the model providers' keys and the database's URL, which a
  process of its own is not handed (below);
- a handler that blocks, or brings its interpreter down, takes a worker with
  it, not the server.

It costs one process and a long-poll over the loopback interface.

### The Flows pages

The session (`GET /auth/session`) carries `flows_url`, `/neorc/ui/`, when the
server mounts neorc, and nothing otherwise. The interface then has one panel
and one menu button for both: a **Flows** section in its left panel, below the
history, is neorc's navigation, and neorc's UI, framed where the chat would
be, is opened with `?embedded`, which leaves out neorc's own sidebar.

The section holds what neorc's sidebar holds: **New run**, **Runs**, neorc's
flows and its six latest runs, each status a dot. It reads them from neorc's
API, one level above `flows_url`, and reads them again whenever neorc records
an event, which it long-polls for (`frontend/src/flows/orchestrator.ts`). In
the rail, New run and Runs stay as icons.

Each page is a route of the shell's, translated to neorc's own hash in the
frame:

| route | neorc's page |
|---|---|
| `#/flows` | `#/runs` |
| `#/flows/new` | `#/flows` |
| `#/flows/runs/<id>` | `#/runs/<id>` |
| `#/flows/flow/<name>` | `#/flows/<name>` |

The frame is loaded once and then moved by its hash, both ways: a route of the
shell's replaces the frame's hash, and a move inside the frame, such as a run
started from its form, replaces the shell's route, so the address bar and the
panel say where the frame is and Back undoes both at once. neorc's UI is built
to work under any prefix: it finds its API one level above its page, and
routes by its hash.

The UI's answers say `frame-ancestors 'self'` and `x-frame-options:
SAMEORIGIN` in the place of neorc's `'none'`, so the interface, and nothing on
another origin, may frame it; the rest of neorc's policy is kept. Every other
answer under `/neorc` carries this server's headers, `DENY` included.

### Where neorc may be imported

In `cli/` alone, the composition root (an import-linter contract in
`backend/pyproject.toml`, and [rules.md](rules.md)); in practice in
`cli/orchestration.py`, which `cli/main.py` calls. `web/` knows a URL to put in
the session and nothing else, and `controller/` nothing. Taking neorc out is
taking out that module and its call.

### Installing neorc

Robinauts depends on `neorc[http,manager,postgres]`, `neorc-core` and
`neorc-ui`. neorc publishes no wheels yet. Robinauts builds them from a commit
of open-shipyard/neorc's `main`, named in `[tool.uv.sources]` in
`backend/pyproject.toml`.

uv builds these three packages from that commit. Every other package stays under
`--no-build`. The build needs Node.js, at the version `frontend/.nvmrc` names.
`scripts/neorc-from-source.sh` builds the wheels of a release, and refuses a
commit outside neorc's `main`. The extra workers' images leave neorc out.

When neorc publishes wheels, take this step out:

1. `git grep neorc-from-source` lists every part of it. Remove each one.
2. Point the three dependencies at the published wheels.
3. Run `uv lock` in `backend/` and in each extra worker.

neorc's PostgreSQL adapter is on asyncpg: psycopg is LGPL-3.0, which
[DEPENDENCIES.md](../../DEPENDENCIES.md) forbids.

### What the worker is handed

The worker is handed the server's URL in `ROBINAUTS_API_URL`, which is how a
task reaches this server's API, and no credential, whatever the shell held in
`ROBINAUTS_API_TOKEN`: the local development mode has no sign-in, every request
is its one user's, and what a flow starts is the developer's, as what the
browser starts is.

neorc's worker is not handed what the configuration keeps from everything but
the server: the model providers' keys, the tool servers' secrets,
`ROBINAUTS_DATABASE_URL` and `ROBINAUTS_CONFIG`.

### The flows Robinauts ships

[orchestration/](../../orchestration/README.md) holds them, with their
handlers: plain functions that use the standard library alone and import
neither neorc nor Robinauts.

- `guess_number`: a guessing game between two conversations with the first
  agent the server offers.
