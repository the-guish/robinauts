# Orchestration

One of Robinauts foundationals premises is to be well architected to support multiple deployment modes with the same code base, and to be modular enough to be partially adopted as component on other systems. It aims to be deployable as a multi-user platform for a company, but also be fully reused as a desktop application.

[Neorc](https://github.com/open-shipyard/neorc) aims to be lightweight orchestration layer with minimal dependencies that also can be deployed to cluster or be embedded in smaller applications.


They integrate together to server multiple scenarios.

On dev mode or desktop application, Robinauts starts a local instance of Neorc that users can take for orchestrating their own local workflows.


On a cluster Neorc would be started on its own and connected to a running instance of Robinauts, creating sessions with a system user provided by Robinauts.


## Local development: neorc inside `robinauts start`

A flows folder turns it on (below, [Two configurations](#two-configurations-agents-and-flows)).
`robinauts start --dev-no-sign-in` then embeds neorc in its own process and on
its own port
([orchestrator/adapters/neorc/embedded.py](../../backend/src/robinauts/orchestrator/adapters/neorc/embedded.py)):

1. neorc's **manager** is an application mounted at `/neorc`, its UI at
   `/neorc/ui/`, with no authentication of its own, as the local development
   mode has none. Every request to it passes this server's checks first: the
   mode's `Host` and `Origin` rules reach `/neorc` as they reach every path;
2. its **storage** follows this server's: in this process without
   `ROBINAUTS_DATABASE_URL`, gone at the stop; on that PostgreSQL with it, in a
   schema of its own, `neorc`, made at the start if it is not there. Its tables
   are named `neorc_*` besides, so none meets one of this server's;
3. the flows in the flows folder are **uploaded**, at every start;
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

With sign-in on, a flows folder is refused for now: neorc's manager would be
served with no authentication. On a cluster neorc is started on its own.

### Two configurations: agents and flows

The agents and the flows are configured apart, each in a place of its own:

| | named by | holds |
|---|---|---|
| **agents** | `ROBINAUTS_CONFIG`, a TOML file | model providers, models, tool servers, agents, workers, sign-in: everything Robinauts has without neorc |
| **flows** | `ROBINAUTS_FLOWS_DIR`, a folder | the flow files (`*.yaml`, `*.yml`), uploaded to neorc as they are; their forms, `<flow>.form.toml`; and `flows.toml`, their settings and repositories |

Without `ROBINAUTS_FLOWS_DIR` there is no orchestrator. The agents' file knows
nothing of the flows: an `[orchestration]` table in it, where the flows'
settings were before they had a folder, is refused with where they went.

`flows.toml` is optional; these are its keys and their defaults (and its
`[repositories]`, under [Flows as forms](#flows-as-forms)):

```toml
code_location = "handlers"   # the handlers; default: this checkout's orchestration/
ui = true                    # serve neorc's UI at /neorc/ui/; the default
```

A relative `code_location` starts at the flows folder. A folder of a person's
own flows, anywhere, may call the handlers Robinauts ships, which are the
default:

```bash
ROBINAUTS_FLOWS_DIR=~/robinauts/flows robinauts start --dev-no-sign-in
```

### Flows as forms

The interface starts a flow from a form of its own, not from neorc's: the
server lists every flow of the flows folder with its inputs, and the values
each may take, and starts a run once the inputs fit
([orchestrator/application/flows.py](../../backend/src/robinauts/orchestrator/application/flows.py)).
The inputs go to neorc as they came: neorc knows nothing of the forms, and its
flow files are plain neorc flows.

| route | answer |
|---|---|
| `GET /api/flows` | every flow, with its inputs: a label, the type the flow declares, and the values a list of values offers |
| `POST /api/flows/<name>/runs` | `{"inputs": {...}}`; 201 with the run's id, 404 for a flow the folder does not hold, 422 for inputs that do not fit |

A flow's form is `<flow name>.form.toml`, beside it in the flows folder. It is
optional: without one, every input is a plain field.

```toml
title = "Code and review"

[inputs.task]
label = "Task"

[inputs.repo_branch]
label = "Repository"
lov = "repo_branch"

[inputs.model]
lov = "model"

[inputs.agent]
lov = "agent"
```

The form's inputs come first, in its order, then those it leaves out. An input
it names must be one its flow declares, and an input with a list of values
must be a `string`: a form that does not fit its flow is refused at the start.
There are three lists of values:

| `lov` | the values | the value sent |
|---|---|---|
| `repo_branch` | each branch of each repository of `flows.toml` | `<id>@<branch>`, the repository by its id there |
| `model` | the models of the agents' configuration | the model's id |
| `agent` | its agents, each labelled with the worker it runs on | the agent's id |

The repositories are the flows' configuration, in `flows.toml`:

```toml
[repositories.robinauts]
url = "https://github.com/the-guish/robinauts"
branches = ["main"]
```

A model the chosen agent's engine cannot reach is not refused yet: that needs
the engines' provider kinds, which the orchestrator does not see.

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
and one menu button for both: the left panel's **Flows** tab, beside its
**Sessions** tab, is neorc's navigation, and neorc's UI, framed where the chat
would be, is opened with `?embedded`, which leaves out neorc's own sidebar.

The tab holds what neorc's sidebar holds: **New run**, **Runs**, neorc's
flows and its six latest runs, each status a dot. It reads them from neorc's
API, one level above `flows_url`, and reads them again whenever neorc records
an event, which it long-polls for (`frontend/src/flows/orchestrator.ts`). In
the rail, New run and Runs stay as icons.

Starting a run is the interface's own: **New run** lists the flows, and each
flow's form is a page of its own, over `/api/flows` ([Flows as
forms](#flows-as-forms), `frontend/src/flows/NewRunPage.tsx`). A choice from
its list for an input with one, a field of its type for any other, every
input required. Once the run starts, the interface goes to it, in the frame.

The other pages are neorc's, framed; each is a route of the shell's,
translated to neorc's own hash in the frame:

| route | page |
|---|---|
| `#/flows` | neorc's `#/runs` |
| `#/flows/new` | the interface's list of flows |
| `#/flows/new/<name>` | the interface's form of that flow |
| `#/flows/runs/<id>` | neorc's `#/runs/<id>` |
| `#/flows/flow/<name>` | neorc's `#/flows/<name>` |

A move inside the frame to neorc's own list of flows, `#/flows`, is the
interface's `#/flows/new`: a run is started from the interface's forms.

The frame is loaded once and then moved by its hash, both ways: a route of the
shell's replaces the frame's hash, and a move inside the frame, such as a run
opened from the list, replaces the shell's route, so the address bar and the
panel say where the frame is and Back undoes both at once. neorc's UI is built
to work under any prefix: it finds its API one level above its page, and
routes by its hash.

The UI's answers say `frame-ancestors 'self'` and `x-frame-options:
SAMEORIGIN` in the place of neorc's `'none'`, so the interface, and nothing on
another origin, may frame it; the rest of neorc's policy is kept. Every other
answer under `/neorc` carries this server's headers, `DENY` included.

### Where neorc may be imported

In `orchestrator/adapters/neorc/` alone (an import-linter contract in
`backend/pyproject.toml`, and [rules.md](rules.md)). The orchestrator is a
sibling of the controller: it may see the controller's contract, and the
controller sees nothing of it. `cli/orchestration.py` builds it, and
`cli/main.py` alone imports that module; `cli/main.py` otherwise sees the
orchestrator's contract alone, an `Orchestrator` to mount, start and stop, and
the `Flows` that `web/` serves. `web/` sees that contract and nothing else.
Taking neorc out is taking out that package and `cli/orchestration.py`.

`cli/orchestration.py` is apart from `cli/composition.py` because a worker in
a process of its own imports the composition, and the extra workers' images
leave neorc out. The extra workers also read `[repositories]` of `flows.toml`
with the orchestrator's own parser, `orchestrator/core/config.py`. A contract
keeps all of it so: nothing `cli/worker_process.py`, `cli/composition.py`,
`orchestrator/contract/` or `orchestrator/core/config.py` imports may reach
neorc, even through another module.

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

neorc's worker is not handed what the agents' configuration keeps from
everything but the server: the model providers' keys, the tool servers'
secrets, `ROBINAUTS_DATABASE_URL` and `ROBINAUTS_CONFIG`.

### The flows Robinauts ships

[orchestration/](../../orchestration/README.md) holds them, with their
handlers: plain functions that use the standard library alone and import
neither neorc nor Robinauts.

- `guess_number`: a guessing game between two conversations with the first
  agent the server offers.
- `code_and_review`: a task done on a repository and reviewed, from a form
  (`code_and_review.form.toml`): the chosen agent, on the chosen model, does
  the task in its session's worktree of the chosen repository, from the chosen
  branch, pushes the session's branch and opens a pull request against the
  chosen branch; a second conversation reads the pull request in its own
  worktree and reviews it against the task. Each answer ends with a `RESULT`
  line of JSON the handler reads: the pull request's URL and branch, then the
  verdict, `approve` or `request_changes`, which is the run's output. The
  worktrees, their branches and the push are the extra worker's, from the same
  `[repositories]` the form offers
  ([extra-workers/worker-common](../../extra-workers/worker-common/README.md),
  "Repositories and worktrees"); the handlers hold no credential.
